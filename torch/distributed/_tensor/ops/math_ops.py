# Copyright (c) Meta Platforms, Inc. and affiliates
from dataclasses import dataclass
from enum import Enum
from typing import cast, List, Optional, Sequence, Tuple, Union

import torch
import torch.distributed._functional_collectives as funcol

import torch.distributed.distributed_c10d as c10d
from torch.distributed._tensor.op_schema import (
    OpSchema,
    OpStrategy,
    PlacementStrategy,
    RuntimeSchemaInfo,
)
from torch.distributed._tensor.ops.utils import (
    as_list,
    generate_redistribute_costs,
    is_tensor_evenly_shardable,
    normalize_dim,
    normalize_dims,
    normalize_to_torch_size,
    register_op_strategy,
)
from torch.distributed._tensor.placement_types import (
    _Partial,
    DTensorSpec,
    Placement,
    Replicate,
    Shard,
)
from torch.distributed.device_mesh import DeviceMesh


aten = torch.ops.aten


class Reduction(Enum):
    NONE = 0
    MEAN = 1
    SUM = 2


@dataclass(frozen=True)
class NormReduction:
    norm_type: Union[int, float, str]


ReductionOpType = Union[NormReduction, c10d.ReduceOp.RedOpType]


def _infer_reduction_dims(dims_arg: object, ndim: int) -> Optional[List[int]]:
    if dims_arg is None:
        return None
    dims = cast(List[int], as_list(dims_arg))
    dims = cast(List[int], normalize_dims(dims, ndim))
    empty_dims = [[0], [-1], []]
    if ndim == 0 and dims_arg in empty_dims:
        return None
    return dims


def _infer_reduce_dims_map(
    reduction_dims: List[int], input_ndim: int, keep_dim=False
) -> List[int]:
    reduction_dims_map = []
    new_dim_count = 0
    for input_dim in range(input_ndim):
        if input_dim in reduction_dims and not keep_dim:
            # if input dim in reduction dims, mark it as -1
            reduction_dims_map.append(-1)
        else:
            # otherwise mark it as the new dim
            reduction_dims_map.append(new_dim_count)
            new_dim_count += 1

    return reduction_dims_map


def replicate_reduction_dims(
    placements: Tuple[Placement, ...], reduction_dims: List[int]
) -> Tuple[Placement, ...]:
    # replicate the reduction dims if not reduction_linear
    new_placements: List[Placement] = []

    for p in placements:
        if p.is_partial():
            new_placements.append(Replicate())
        elif isinstance(p, Shard) and p.dim in reduction_dims:
            new_placements.append(Replicate())
        else:
            new_placements.append(p)

    return tuple(new_placements)


def map_placements_after_reduction(
    placements: Tuple[Placement, ...],
    reduction_dims: List[int],
    reduction_dims_map: List[int],
    reduction_op: ReductionOpType,
) -> Tuple[Placement, ...]:
    """
    Map each placement based on the output shape after reduction.
    """
    new_placements: List[Placement] = []
    for placement in placements:
        if isinstance(placement, (Replicate, _Partial)):
            new_placements.append(placement)
        else:
            assert isinstance(placement, Shard)
            shard_dim = placement.dim
            new_shard_dim = reduction_dims_map[shard_dim]
            if new_shard_dim == -1 or shard_dim in reduction_dims:
                # if new_shard_dim collapsed or its in the reduction dims
                # (i.e. for the case where keepdims=True), we generate partial
                new_placements.append(get_placement_from_reduction_op(reduction_op))
            else:
                new_placements.append(Shard(new_shard_dim))
    return tuple(new_placements)


def get_placement_from_reduction_op(reduction_op: ReductionOpType) -> Placement:
    if isinstance(reduction_op, NormReduction):
        return _NormPartial(norm_type=reduction_op.norm_type)
    return _Partial(reduction_op)


def common_reduction_strategy(
    mesh: DeviceMesh,
    input_strategy: OpStrategy,
    reduce_dims: List[int],
    keep_dim: bool = False,
    reduction_linear: bool = True,
    reduction_op: ReductionOpType = c10d.ReduceOp.SUM,
) -> OpStrategy:
    """
    reduction_linear means that the reduction `f` follows this rule:
        f([f(a), f(b)]) = f([a, b])

    reduction linear should be super set of linearity.
    """
    # by default follow reduction input strategy
    reduction_strategy = OpStrategy([])

    for strtg in input_strategy.strategies:
        if not reduction_linear:
            # input placements for this strategy should clear out pending sum and sharding
            # on the reduction dimension
            input_placements = replicate_reduction_dims(
                strtg.output_spec.placements, reduce_dims
            )
        else:
            input_placements = strtg.output_spec.placements

        input_spec = DTensorSpec(
            mesh=mesh,
            placements=input_placements,
            tensor_meta=strtg.output_spec.tensor_meta,
        )

        reduce_dims_map = _infer_reduce_dims_map(reduce_dims, input_spec.ndim, keep_dim)
        out_placements = map_placements_after_reduction(
            input_spec.placements, reduce_dims, reduce_dims_map, reduction_op
        )
        redistribute_cost = [generate_redistribute_costs(input_strategy, input_spec)]
        reduction_strategy.strategies.append(
            PlacementStrategy(
                output_specs=DTensorSpec(
                    mesh=mesh,
                    placements=out_placements,
                ),
                input_specs=(input_spec,),
                redistribute_cost=redistribute_cost,
            )
        )

    return reduction_strategy


LINEAR_REDUCTION_OP_MAP = {
    aten.all.default: c10d.ReduceOp.SUM,
    aten.all.dim: c10d.ReduceOp.SUM,
    aten.sum.default: c10d.ReduceOp.SUM,
    aten.sum.dim_IntList: c10d.ReduceOp.SUM,
    aten.prod.default: c10d.ReduceOp.PRODUCT,
    aten.prod.dim_int: c10d.ReduceOp.PRODUCT,
    aten.prod.int_out: c10d.ReduceOp.PRODUCT,
    aten.mean.default: c10d.ReduceOp.AVG,
    aten.mean.dim: c10d.ReduceOp.AVG,
    aten.mean.out: c10d.ReduceOp.AVG,
    aten.max.default: c10d.ReduceOp.MAX,
    aten.max.dim: c10d.ReduceOp.MAX,
    aten.max.out: c10d.ReduceOp.MAX,
    aten.min.default: c10d.ReduceOp.MIN,
    aten.min.dim: c10d.ReduceOp.MIN,
    aten.min.out: c10d.ReduceOp.MIN,
}


@register_op_strategy(
    list(LINEAR_REDUCTION_OP_MAP.keys()), schema_info=RuntimeSchemaInfo(1)
)
def linear_reduction_strategy(mesh: DeviceMesh, op_schema: OpSchema) -> OpStrategy:
    args_schema = op_schema.args_schema
    input_strategy = args_schema[0]
    assert isinstance(input_strategy, OpStrategy)
    dims = None
    if len(op_schema.args_schema) > 1:
        dims = _infer_reduction_dims(args_schema[1], input_strategy.output_ndim)

    reduce_dims = list(range(input_strategy.output_ndim)) if dims is None else dims

    keep_dim = len(op_schema.args_schema) > 2 and bool(op_schema.args_schema[2])
    reduction_op = LINEAR_REDUCTION_OP_MAP[op_schema.op]
    return common_reduction_strategy(
        mesh,
        input_strategy,
        reduce_dims,
        keep_dim=keep_dim,
        reduction_linear=True,
        reduction_op=reduction_op,
    )


@register_op_strategy(
    [aten.var.correction, aten.var.correction_out],
    schema_info=RuntimeSchemaInfo(1, ["keepdim"]),
)
def var_reduction_strategy(mesh: DeviceMesh, op_schema: OpSchema) -> OpStrategy:
    args_schema = op_schema.args_schema
    input_strategy = args_schema[0]
    assert isinstance(input_strategy, OpStrategy)
    dims = None
    if len(op_schema.args_schema) > 1:
        dims = _infer_reduction_dims(args_schema[1], input_strategy.output_ndim)

    reduce_dims = list(range(input_strategy.output_ndim)) if dims is None else dims

    keep_dim = cast(bool, op_schema.kwargs_schema.get("keepdim", False))
    return common_reduction_strategy(
        mesh, input_strategy, reduce_dims, keep_dim=keep_dim, reduction_linear=False
    )


@dataclass(frozen=True)
class _NormPartial(_Partial):
    norm_type: Union[int, float, str] = 2

    def _partition_value(self):
        raise NotImplementedError()

    def _reduce_shard_value(self):
        raise NotImplementedError()

    def _reduce_value(
        self, tensor: torch.Tensor, mesh: DeviceMesh, mesh_dim: int
    ) -> torch.Tensor:
        if self.norm_type in (float("inf"), "inf"):
            return funcol.all_reduce(tensor, reduceOp="max", group=(mesh, mesh_dim))
        elif self.norm_type in (float("-inf"), "-inf"):
            return funcol.all_reduce(tensor, reduceOp="min", group=(mesh, mesh_dim))
        elif self.norm_type == 0:
            raise NotImplementedError()
        elif isinstance(self.norm_type, (int, float)):
            reduced_tensor = funcol.all_reduce(
                tensor**self.norm_type, reduceOp="sum", group=(mesh, mesh_dim)
            )
            if self.norm_type != 1:
                reduced_tensor **= 1.0 / self.norm_type
            return reduced_tensor
        else:
            raise NotImplementedError(f"norm type: {self.norm_type}")


@register_op_strategy(
    [aten.linalg_vector_norm.default], schema_info=RuntimeSchemaInfo(1)
)
def vector_norm_strategy(mesh: DeviceMesh, op_schema: OpSchema) -> OpStrategy:
    args_schema = op_schema.args_schema
    input_strategy = args_schema[0]
    assert isinstance(input_strategy, OpStrategy)
    norm_type = args_schema[1] if len(args_schema) > 1 else 2
    assert isinstance(norm_type, (int, float, str)), f"{norm_type}"
    dim = args_schema[2] if len(args_schema) > 2 else None
    keepdim = args_schema[3] if len(args_schema) > 3 else False
    dims = _infer_reduction_dims(dim, input_strategy.output_ndim)
    reduce_dims = list(range(input_strategy.output_ndim)) if dims is None else dims
    return common_reduction_strategy(
        mesh,
        input_strategy,
        reduce_dims,
        keep_dim=cast(bool, keepdim),
        reduction_linear=True,
        reduction_op=NormReduction(norm_type),
    )


@register_op_strategy(
    [aten._log_softmax.default, aten._softmax.default], schema_info=RuntimeSchemaInfo(1)
)
def softmax_strategy(mesh: DeviceMesh, op_schema: OpSchema) -> OpStrategy:
    input_strategy, softmax_dim, _ = op_schema.args_schema
    input_strategy = cast(OpStrategy, input_strategy)
    softmax_dim = cast(int, softmax_dim)
    softmax_dim = normalize_dim(softmax_dim, input_strategy.output_ndim)

    output_strategy = OpStrategy([])
    for idx, input_placement_strategy in enumerate(input_strategy.strategies):
        redistribute_costs = []
        input_src_spec = input_placement_strategy.output_spec

        # make sure input is replicated along the softmax dim
        input_target_spec = DTensorSpec(
            mesh=mesh,
            placements=replicate_reduction_dims(
                input_src_spec.placements, [softmax_dim]
            ),
            tensor_meta=input_src_spec.tensor_meta,
        )
        redistribute_costs.append(
            generate_redistribute_costs(input_strategy, input_target_spec)
        )
        output_target_spec = input_target_spec
        output_strategy.strategies.append(
            PlacementStrategy(
                output_specs=output_target_spec,
                input_specs=[input_target_spec],
                redistribute_cost=redistribute_costs,
            )
        )

    return output_strategy


@register_op_strategy(
    [
        aten._log_softmax_backward_data.default,
        aten._softmax_backward_data.default,
    ],
    schema_info=RuntimeSchemaInfo(2),
)
def softmax_backward_strategy(mesh: DeviceMesh, op_schema: OpSchema) -> OpStrategy:
    grad_out_strategy, out_strategy, softmax_dim, _ = op_schema.args_schema
    grad_out_strategy = cast(OpStrategy, grad_out_strategy)
    out_strategy = cast(OpStrategy, out_strategy)
    softmax_dim = cast(int, softmax_dim)
    softmax_dim = normalize_dim(softmax_dim, grad_out_strategy.output_ndim)

    grad_in_strategy = OpStrategy([])
    for grad_out_placement_strat, out_placement_strat in zip(
        grad_out_strategy.strategies, out_strategy.strategies
    ):
        # follow the sharding of the grad_out or out depending on which has more shards
        grad_out_src_spec = grad_out_placement_strat.output_spec
        out_src_spec = out_placement_strat.output_spec
        src_spec = (
            grad_out_src_spec
            if grad_out_src_spec.num_shards >= out_src_spec.num_shards
            else out_src_spec
        )

        # make sure inputs are replicated along the softmax dim
        tgt_spec = DTensorSpec(
            mesh=mesh,
            placements=replicate_reduction_dims(src_spec.placements, [softmax_dim]),
        )
        redist_grad_out_cost = generate_redistribute_costs(grad_out_strategy, tgt_spec)
        redist_out_cost = generate_redistribute_costs(out_strategy, tgt_spec)
        grad_in_strategy.strategies.append(
            PlacementStrategy(
                output_specs=tgt_spec,
                redistribute_cost=[redist_grad_out_cost, redist_out_cost],
            )
        )

    return grad_in_strategy


@register_op_strategy(
    [aten.nll_loss_forward.default, aten.nll_loss2d_forward.default],
    schema_info=RuntimeSchemaInfo(3),
)
def nll_loss_forward_strategy(mesh: DeviceMesh, op_schema: OpSchema) -> OpStrategy:
    assert len(op_schema.args_schema) == 5
    (
        input_strategy,
        target_strategy,
        weight_strategy,
        reduction,
        _,
    ) = op_schema.args_schema
    input_strategy = cast(OpStrategy, input_strategy)
    target_strategy = cast(OpStrategy, target_strategy)
    reduction = cast(int, reduction)

    input_shape = input_strategy.output_shape
    channel_dim = 1 if len(input_shape) >= 2 else 0

    output_strategy = OpStrategy([])
    for idx, input_placement_strategy in enumerate(input_strategy.strategies):
        op_args_target_specs = []
        redistribute_costs = []

        # make sure input is replicated along the channel dim
        input_src_spec = input_placement_strategy.output_spec
        input_expected_spec = DTensorSpec(
            mesh=mesh,
            placements=replicate_reduction_dims(
                input_src_spec.placements, [channel_dim]
            ),
            tensor_meta=input_src_spec.tensor_meta,
        )
        op_args_target_specs.append(input_expected_spec)
        redistribute_costs.append(
            generate_redistribute_costs(input_strategy, input_expected_spec)
        )

        # target doesn't have channel dim, and it follows input on other dims
        target_src_spec = target_strategy.strategies[idx].output_spec
        target_expected_spec = DTensorSpec(
            mesh=mesh,
            placements=_skip_dim(input_expected_spec.placements, channel_dim),
            tensor_meta=target_src_spec.tensor_meta,
        )
        op_args_target_specs.append(target_expected_spec)
        redistribute_costs.append(
            generate_redistribute_costs(target_strategy, target_expected_spec)
        )

        # weight tensor, if given, has to be a Tensor of size input_shape[channel_dim]
        # make sure it is replicated
        if weight_strategy is not None:
            assert isinstance(weight_strategy, OpStrategy)
            weight_src_spec = weight_strategy.strategies[idx].output_spec
            weight_expected_spec = DTensorSpec(
                mesh=mesh,
                placements=_replicate_dims_start_at(weight_src_spec.placements),
                tensor_meta=weight_src_spec.tensor_meta,
            )
            op_args_target_specs.append(weight_expected_spec)
            redistribute_costs.append(
                generate_redistribute_costs(weight_strategy, weight_expected_spec)
            )

        if reduction == Reduction.NONE.value:
            output_expected_spec = target_expected_spec
            total_weight_expected_spec = DTensorSpec(
                mesh=mesh, placements=tuple([Replicate()] * mesh.ndim)
            )
        else:
            if reduction == Reduction.MEAN.value:
                reduction_op = c10d.ReduceOp.AVG
                if not is_tensor_evenly_shardable(
                    target_expected_spec.shape, target_expected_spec
                ):
                    raise ValueError(
                        "The intermediate results of nll_loss cannot be evenly sharded, \
                        resulting in biased mean result."
                    )
            else:  # reduction == Reduction.SUM.value:
                reduction_op = c10d.ReduceOp.SUM
            reduce_dims = list(range(target_expected_spec.ndim))
            reduce_dims_map = _infer_reduce_dims_map(
                reduce_dims, target_expected_spec.ndim, keep_dim=False
            )
            out_placements = map_placements_after_reduction(
                target_expected_spec.placements,
                reduce_dims,
                reduce_dims_map,
                reduction_op,
            )
            output_expected_spec = DTensorSpec(
                mesh=mesh,
                placements=out_placements,
            )

            # whether reduction is sum or mean, the total weight has to be summed up if not replicated
            total_weight_placements = map_placements_after_reduction(
                target_expected_spec.placements,
                reduce_dims,
                reduce_dims_map,
                c10d.ReduceOp.SUM,
            )
            total_weight_expected_spec = DTensorSpec(
                mesh=mesh,
                placements=total_weight_placements,
            )

        output_strategy.strategies.append(
            PlacementStrategy(
                output_specs=(output_expected_spec, total_weight_expected_spec),
                input_specs=op_args_target_specs,
                redistribute_cost=redistribute_costs,
            )
        )

    return output_strategy


@register_op_strategy(
    [aten.nll_loss_backward.default, aten.nll_loss2d_backward.default],
    schema_info=RuntimeSchemaInfo(4),
)
def nll_loss_backward_strategy(mesh: DeviceMesh, op_schema: OpSchema) -> OpStrategy:
    assert len(op_schema.args_schema) == 7
    (
        grad_out_strategy,
        input_strategy,
        target_strategy,
        weight_strategy,
        reduction,
        _,
        total_weight_strategy,
    ) = op_schema.args_schema
    grad_out_strategy = cast(OpStrategy, grad_out_strategy)
    input_strategy = cast(OpStrategy, input_strategy)
    target_strategy = cast(OpStrategy, target_strategy)
    reduction = cast(int, reduction)
    total_weight_strategy = cast(OpStrategy, total_weight_strategy)

    input_shape = input_strategy.output_shape
    channel_dim = 1 if len(input_shape) >= 2 else 0

    grad_in_strategy = OpStrategy([])
    for idx, input_placement_strategy in enumerate(input_strategy.strategies):
        op_args_target_specs = []
        redistribute_costs = []

        # make sure input is replicated along the channel dim
        input_src_spec = input_placement_strategy.output_spec
        input_expected_spec = DTensorSpec(
            mesh=mesh,
            placements=replicate_reduction_dims(
                input_src_spec.placements, [channel_dim]
            ),
            tensor_meta=input_src_spec.tensor_meta,
        )
        op_args_target_specs.append(input_expected_spec)
        redistribute_costs.append(
            generate_redistribute_costs(input_strategy, input_expected_spec)
        )

        # target doesn't have channel dim, and it follows input on other dims
        target_src_spec = target_strategy.strategies[idx].output_spec
        target_expected_spec = DTensorSpec(
            mesh=mesh,
            placements=_skip_dim(input_expected_spec.placements, channel_dim),
            tensor_meta=target_src_spec.tensor_meta,
        )
        op_args_target_specs.append(target_expected_spec)
        redistribute_costs.append(
            generate_redistribute_costs(target_strategy, target_expected_spec)
        )

        # grad_out follows target if there is no reduction;
        # otherwise, it should be a replicated scalar.
        grad_out_src_spec = grad_out_strategy.strategies[idx].output_spec
        if reduction == Reduction.NONE.value:
            grad_out_expected_spec = target_expected_spec
        else:
            grad_out_expected_spec = DTensorSpec(
                mesh=mesh,
                placements=_replicate_dims_start_at(grad_out_src_spec.placements),
                tensor_meta=grad_out_src_spec.tensor_meta,
            )
        op_args_target_specs.insert(0, grad_out_expected_spec)
        redistribute_costs.insert(
            0, generate_redistribute_costs(grad_out_strategy, grad_out_expected_spec)
        )

        # weight tensor, if given, has to be a Tensor of size input_shape[channel_dim]
        # make sure it is replicated
        if weight_strategy is not None:
            assert isinstance(weight_strategy, OpStrategy)
            weight_src_spec = weight_strategy.strategies[idx].output_spec
            weight_expected_spec = DTensorSpec(
                mesh=mesh,
                placements=_replicate_dims_start_at(weight_src_spec.placements),
                tensor_meta=weight_src_spec.tensor_meta,
            )
            op_args_target_specs.append(weight_expected_spec)
            redistribute_costs.append(
                generate_redistribute_costs(weight_strategy, weight_expected_spec)
            )

        # total_weight should always be replicated
        total_weight_src_spec = total_weight_strategy.strategies[idx].output_spec
        total_weight_expected_spec = DTensorSpec(
            mesh=mesh,
            placements=_replicate_dims_start_at(total_weight_src_spec.placements),
            tensor_meta=total_weight_src_spec.tensor_meta,
        )
        op_args_target_specs.append(total_weight_expected_spec)
        redistribute_costs.append(
            generate_redistribute_costs(
                total_weight_strategy, total_weight_expected_spec
            )
        )

        grad_in_expected_spec = input_expected_spec
        grad_in_strategy.strategies.append(
            PlacementStrategy(
                output_specs=grad_in_expected_spec,
                input_specs=op_args_target_specs,
                redistribute_cost=redistribute_costs,
            )
        )

    return grad_in_strategy


@register_op_strategy(
    [aten.native_layer_norm.default],
    schema_info=RuntimeSchemaInfo(1),
)
def layer_norm_strategy(mesh: DeviceMesh, op_schema: OpSchema) -> OpStrategy:
    # args must be: input, normalized_shape, weight, bias, eps
    # for None weight and bias, their corresponding objects will
    # be None as well. layer_norm_strategy returns one OpStrategy
    # for the triple return values (out, mean, rstd).
    assert len(op_schema.args_schema) == 5
    (
        input_strategy,
        normalized_shape,
        weight_strategy,
        bias_strategy,
        _,
    ) = op_schema.args_schema

    # the current layer norm implementation requires that all
    # input DTensor's sharding must be in form of OpStrategy
    assert isinstance(input_strategy, OpStrategy)
    assert isinstance(normalized_shape, (int, Sequence, torch.Size))
    normalized_size = normalize_to_torch_size(normalized_shape)

    input_ndim = input_strategy.output_ndim
    axis = input_ndim - len(normalized_size)

    # we use OpStrategy because the output (out, mean, rstd)
    # should have the same placements
    output_strategy = OpStrategy([])
    for idx, input_placement_strategy in enumerate(input_strategy.strategies):
        op_args_target_specs = []
        redistribute_costs = []
        input_src_spec = input_placement_strategy.output_spec

        # for the input tensor, we replicate it on the inner dims if necessary
        # TODO: we can avoid forcing the redistribution once we figure out
        # how to decompose layer norm
        input_target_spec = DTensorSpec(
            mesh=mesh,
            placements=_replicate_dims_start_at(input_src_spec.placements, axis),
            tensor_meta=input_src_spec.tensor_meta,
        )
        op_args_target_specs.append(input_target_spec)
        redistribute_costs.append(
            generate_redistribute_costs(input_strategy, input_target_spec)
        )

        if weight_strategy is not None:
            assert isinstance(weight_strategy, OpStrategy)
            weight_src_spec = weight_strategy.strategies[idx].output_spec

            # for the weight tensor, we replicate it on all dims if necessary
            # TODO: we can avoid forcing the redistribution once we figure out
            # how to decompose layer norm
            weight_target_spec = DTensorSpec(
                mesh=mesh,
                placements=_replicate_dims_start_at(weight_src_spec.placements),
                tensor_meta=weight_src_spec.tensor_meta,
            )
            op_args_target_specs.append(weight_target_spec)
            redistribute_costs.append(
                generate_redistribute_costs(weight_strategy, weight_target_spec)
            )

        if bias_strategy is not None:
            assert isinstance(bias_strategy, OpStrategy)
            bias_src_spec = bias_strategy.strategies[idx].output_spec

            # for the bias tensor, we replicate it on all dims if necessary
            # TODO: we can avoid forcing the redistribution once we figure out
            # how to decompose layer norm
            bias_target_spec = DTensorSpec(
                mesh=mesh,
                placements=_replicate_dims_start_at(bias_src_spec.placements),
                tensor_meta=bias_src_spec.tensor_meta,
            )
            op_args_target_specs.append(bias_target_spec)
            redistribute_costs.append(
                generate_redistribute_costs(bias_strategy, bias_target_spec)
            )

        # the output spec is the same as input spec
        output_target_spec = input_target_spec
        output_strategy.strategies.append(
            PlacementStrategy(
                output_specs=output_target_spec,
                input_specs=op_args_target_specs,
                redistribute_cost=redistribute_costs,
            )
        )

    return output_strategy


@register_op_strategy(
    [aten.native_layer_norm_backward.default],
    schema_info=RuntimeSchemaInfo(2),
)
def layer_norm_bwd_strategy(mesh: DeviceMesh, op_schema: OpSchema) -> OpStrategy:
    # args must be: grad_out, input, normalized_shape, mean, rstd,
    # weight, bias, output_mask. For None weight and bias, their
    # corresponding objects will be None as well.
    assert len(op_schema.args_schema) == 8
    (
        grad_out_strategy,
        input_strategy,
        normalized_shape,
        mean_strategy,
        rstd_strategy,
        weight_strategy,
        bias_strategy,
        output_mask,
    ) = op_schema.args_schema

    assert isinstance(grad_out_strategy, OpStrategy)
    assert isinstance(input_strategy, OpStrategy)
    assert isinstance(mean_strategy, OpStrategy)
    assert isinstance(rstd_strategy, OpStrategy)

    assert isinstance(normalized_shape, (int, Sequence, torch.Size))
    normalized_size = normalize_to_torch_size(normalized_shape)
    input_ndim = input_strategy.output_ndim
    axis = input_ndim - len(normalized_size)
    outer_dims = list(range(axis))

    assert isinstance(output_mask, List) and len(output_mask) == 3

    # output triple: (d_input, d_weight, d_bias)
    out_tuple_strategy = OpStrategy([])
    for idx, input_placement_strategy in enumerate(input_strategy.strategies):
        # args for PlacementStrategy
        output_specs_list: List[Optional[DTensorSpec]] = []
        op_args_target_specs = []
        redistribute_costs = []

        input_src_spec = input_placement_strategy.output_spec
        # arg: grad_out
        # TODO: change the strategy to the following rule.
        # d_input is basically a product of element-wise mul of
        # grad_out, rstd, and normalized input, among which rstd
        # and normalized input (x_hat) should have the same sharding
        # placements, and grad_out's sharding is determined by the
        # pointwise result of x_hat and weight/bias.
        if output_mask[0]:
            # TODO: now grad_out spec follows input spec. we may need
            # to change it to apply a pointwise rule over grad_out,
            # input, and weight.
            grad_out_target_spec = DTensorSpec(
                mesh=mesh,
                placements=_replicate_dims_start_at(input_src_spec.placements, axis),
                tensor_meta=input_src_spec.tensor_meta,
            )
            op_args_target_specs.append(grad_out_target_spec)
            redistribute_costs.append(
                generate_redistribute_costs(grad_out_strategy, grad_out_target_spec)
            )
            output_specs_list.append(grad_out_target_spec)
        else:
            output_specs_list.append(None)

        # arg: input
        input_target_spec = DTensorSpec(
            mesh=mesh,
            placements=_replicate_dims_start_at(input_src_spec.placements, axis),
            tensor_meta=input_src_spec.tensor_meta,
        )
        op_args_target_specs.append(input_target_spec)
        redistribute_costs.append(
            generate_redistribute_costs(input_strategy, input_target_spec)
        )

        # arg: mean, rstd
        mean_src_spec = mean_strategy.strategies[idx].output_spec
        op_args_target_specs.append(mean_src_spec)
        redistribute_costs.append([0.0 for _ in mean_strategy.strategies])
        rstd_src_spec = rstd_strategy.strategies[idx].output_spec
        op_args_target_specs.append(rstd_src_spec)
        redistribute_costs.append([0.0 for _ in rstd_strategy.strategies])

        # arg: weight
        # d_weight = sum(grad_out * (input - mean) / rstd, outer_dim, keepdim=False)
        if output_mask[1]:
            assert isinstance(weight_strategy, OpStrategy)
            weight_src_spec = weight_strategy.strategies[idx].output_spec
            # no need to redistribute weight since they should be replicated
            # in forward pass
            op_args_target_specs.append(weight_src_spec)
            redistribute_costs.append([0.0 for _ in weight_strategy.strategies])
            # TODO: now d_weight spec follows input spec w/ a reduction.
            # we may need to change to a pointwise rule over grad_out and
            # input, then apply a reduction.
            inp_placements = _replicate_dims_start_at(input_src_spec.placements, axis)
            reduce_dims_map = _infer_reduce_dims_map(
                outer_dims, input_src_spec.ndim, False
            )
            out_placements = map_placements_after_reduction(
                inp_placements, outer_dims, reduce_dims_map, c10d.ReduceOp.SUM
            )
            output_specs_list.append(
                DTensorSpec(
                    mesh=mesh,
                    placements=out_placements,
                    tensor_meta=weight_src_spec.tensor_meta,
                )
            )
        else:
            output_specs_list.append(None)

        # arg: bias
        # d_bias = sum(grad_out, outer_dim, keepdim=False)
        if output_mask[2]:
            assert isinstance(bias_strategy, OpStrategy)
            bias_src_spec = bias_strategy.strategies[idx].output_spec
            # no need to redistribute weight since they should be replicated
            # in forward pass
            op_args_target_specs.append(bias_src_spec)
            redistribute_costs.append([0.0 for _ in bias_strategy.strategies])
            # Currently we do not support the case where output_mask[0] is False while
            # output_mask[1] is True. But it's easy to support that by accessing
            # grad_out_spec via a local variable rather than the list. We just don't
            # see the case.
            grad_out_spec = output_specs_list[0]
            assert isinstance(grad_out_spec, DTensorSpec)
            # d_bias spec follows a reduction over grad_out
            inp_placements = _replicate_dims_start_at(grad_out_spec.placements, axis)
            reduce_dims_map = _infer_reduce_dims_map(
                outer_dims, grad_out_spec.ndim, False
            )
            out_placements = map_placements_after_reduction(
                inp_placements, outer_dims, reduce_dims_map, c10d.ReduceOp.SUM
            )
            output_specs_list.append(
                DTensorSpec(
                    mesh=mesh,
                    placements=out_placements,
                    tensor_meta=bias_src_spec.tensor_meta,
                )
            )
        else:
            output_specs_list.append(None)

        out_tuple_strategy.strategies.append(
            PlacementStrategy(
                output_specs=tuple(output_specs_list),
                input_specs=op_args_target_specs,
                redistribute_cost=redistribute_costs,
            )
        )

    return out_tuple_strategy


def _replicate_dims_start_at(
    placements: Sequence[Placement], start_dim: int = 0
) -> Tuple[Placement, ...]:
    new_placements: List[Placement] = []
    for p in placements:
        if p.is_partial() or (isinstance(p, Shard) and p.dim >= start_dim):
            new_placements.append(Replicate())  # make it replicate
        else:
            new_placements.append(p)  # keep the placement
    return tuple(new_placements)


# return new_placements which align with placements but skip the skipped_dim
def _skip_dim(
    placements: Tuple[Placement, ...], skipped_dim: int
) -> Tuple[Placement, ...]:
    new_placements: List[Placement] = []
    for p in placements:
        if isinstance(p, Shard) and p.dim >= skipped_dim:
            new_placements.append(Shard(p.dim - 1))
        else:
            new_placements.append(p)
    return tuple(new_placements)
