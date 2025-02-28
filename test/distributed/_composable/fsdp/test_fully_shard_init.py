# Owner(s): ["oncall: distributed"]

import copy
import itertools
import unittest
from typing import List

import torch
import torch.distributed as dist
import torch.nn as nn
from torch.distributed._composable import replicate
from torch.distributed._composable.fsdp import fully_shard
from torch.distributed._composable.fsdp._fsdp_init import (
    _get_managed_modules,
    _get_managed_states,
)
from torch.distributed._composable.fsdp._fsdp_param import ParamModuleInfo
from torch.distributed._composable.fsdp._fsdp_param_group import _get_param_module_infos
from torch.distributed._tensor import (
    DeviceMesh,
    distribute_tensor,
    DTensor,
    Replicate,
    Shard,
)
from torch.distributed._tensor.placement_types import _StridedShard
from torch.distributed.device_mesh import init_device_mesh
from torch.distributed.fsdp._init_utils import (
    _init_inter_node_process_group,
    _init_intra_node_process_group,
)
from torch.distributed.tensor.parallel import (
    ColwiseParallel,
    parallelize_module,
    RowwiseParallel,
)
from torch.testing._internal.common_cuda import TEST_CUDA
from torch.testing._internal.common_fsdp import FSDPTestMultiThread, MLP
from torch.testing._internal.common_utils import run_tests
from torch.testing._internal.distributed._tensor.common_dtensor import (
    ModelArgs,
    Transformer,
    TransformerBlock,
)


class TestFullyShardDeviceTensor(FSDPTestMultiThread):
    """Tests that tensor parameters are moved to the expected device."""

    @property
    def world_size(self) -> int:
        return 1

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_move_states_to_device_tensor(self):
        model = MLP(8, torch.device("cpu"), with_buffer=True)
        for tensor in itertools.chain(model.parameters(), model.buffers()):
            self.assertEqual(tensor.device, torch.device("cpu"))
        fully_shard(model)
        cuda_device = torch.device("cuda", torch.cuda.current_device())
        for tensor in itertools.chain(model.parameters(), model.buffers()):
            self.assertEqual(tensor.device, cuda_device)


class TestFullyShardDeviceDTensor(FSDPTestMultiThread):
    """Tests that DTensor parameters are moved to the expected device."""

    @property
    def world_size(self) -> int:
        return 4

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_move_states_to_device_dtensor_valid(self):
        assert self.world_size >= 4, f"{self.world_size}"
        dp_size = 2
        global_mesh = init_device_mesh(
            "cuda", (dp_size, self.world_size // dp_size), mesh_dim_names=("dp", "tp")
        )
        dp_mesh, tp_mesh = global_mesh["dp"], global_mesh["tp"]
        model = MLP(8, torch.device("cpu"), with_buffer=True)
        parallelize_module(
            model,
            tp_mesh,
            {"in_proj": ColwiseParallel(), "out_proj": RowwiseParallel()},
        )
        cuda_device = torch.device("cuda", torch.cuda.current_device())
        for tensor in itertools.chain(model.parameters(), model.buffers()):
            if isinstance(tensor, DTensor):
                # DTensor constructor moves to the mesh's device
                self.assertEqual(tensor.device, cuda_device)
                self.assertEqual(tensor._local_tensor.device, cuda_device)
            else:
                self.assertEqual(tensor.device, torch.device("cpu"))
        fully_shard(model, mesh=dp_mesh)
        for tensor in itertools.chain(model.parameters(), model.buffers()):
            self.assertEqual(tensor.device, cuda_device)
            if isinstance(tensor, DTensor):
                self.assertEqual(tensor._local_tensor.device, cuda_device)

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_move_states_to_device_dtensor_invalid(self):
        assert self.world_size >= 4, f"{self.world_size}"
        dp_size = 2
        global_cuda_mesh = init_device_mesh(
            "cuda", (dp_size, self.world_size // dp_size), mesh_dim_names=("dp", "tp")
        )
        global_cpu_mesh = init_device_mesh(
            "cpu", (dp_size, self.world_size // dp_size), mesh_dim_names=("dp", "tp")
        )
        dp_mesh = global_cuda_mesh["dp"]
        tp_mesh = global_cpu_mesh["tp"]  # mismatched meshes!
        model = MLP(8, torch.device("cpu"), with_buffer=True)
        parallelize_module(
            model,
            tp_mesh,
            {"in_proj": ColwiseParallel(), "out_proj": RowwiseParallel()},
        )
        for tensor in itertools.chain(model.parameters(), model.buffers()):
            self.assertEqual(tensor.device, torch.device("cpu"))
            if isinstance(tensor, DTensor):
                self.assertEqual(tensor._local_tensor.device, torch.device("cpu"))
        regex = r"Requires DTensor to have mesh of the same type as the FSDP mesh but got cpu for DTensor and cuda for FSDP"
        with self.assertRaisesRegex(ValueError, regex):
            fully_shard(model, mesh=dp_mesh)


class TestFullyShardMeshArg(FSDPTestMultiThread):
    """Tests the ``mesh`` argument."""

    @property
    def world_size(self) -> int:
        return 2

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_invalid_mesh_ndim(self):
        mesh = init_device_mesh("cuda", (self.world_size, 1, 1))
        model = MLP(8)
        regex = r"fully\_shard expects a 1D or 2D DeviceMesh but got DeviceMesh\('cuda', \[\[\[0\]\], \[\[1\]\]\]\)"
        with self.assertRaisesRegex(ValueError, regex):
            fully_shard(model, mesh=mesh)


class TestFullyShardManagedModulesAndStates(FSDPTestMultiThread):
    """Tests getting the managed modules/states for a ``fully_shard`` module."""

    @property
    def world_size(self) -> int:
        return 1

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_managed_modules_single(self):
        model = MLP(8)
        # Assume calling `fully_shard` on `model`
        managed_modules = _get_managed_modules((model,))
        expected_managed_modules = list(model.modules())
        self._check_managed_modules(managed_modules, expected_managed_modules)

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_managed_modules_nested(self):
        model = nn.Sequential(*[MLP(8) for _ in range(2)])
        fully_shard(model[0])
        # Assume calling `fully_shard` on `model`
        managed_modules = _get_managed_modules((model,))
        expected_managed_modules = list(model[1].modules()) + [model]
        self._check_managed_modules(managed_modules, expected_managed_modules)

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_managed_modules_nested_fully_shard_and_replicate(self):
        model = nn.Sequential(*[MLP(8) for _ in range(3)])
        replicate(model[0])
        fully_shard(model[2])
        # Assume calling `fully_shard` on `model`
        managed_modules = _get_managed_modules((model,))
        expected_managed_modules = list(model[1].modules()) + [model]
        self._check_managed_modules(managed_modules, expected_managed_modules)

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_managed_modules_duplicate(self):
        mlp = MLP(8)
        model = nn.Sequential(mlp, mlp)  # duplicate MLP
        # Assume calling `fully_shard` on `model`
        managed_modules = _get_managed_modules((model,))
        # Check that the duplicate module is only counted once
        expected_managed_modules = list(mlp.modules()) + [model]
        self._check_managed_modules(managed_modules, expected_managed_modules)

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_managed_modules_list_of_mlps(self):
        model = nn.Sequential(*[MLP(8) for _ in range(5)])
        # Assume calling `fully_shard` on `[model[0], model[1], model[2]]`
        managed_modules = _get_managed_modules((model[0], model[1], model[2]))
        expected_managed_modules = (
            list(model[0].modules())
            + list(model[1].modules())
            + list(model[2].modules())
        )
        self._check_managed_modules(managed_modules, expected_managed_modules)
        # Assume calling `fully_shard` on `[model[1], model[3]]`
        managed_modules = _get_managed_modules((model[1], model[3]))
        expected_managed_modules = list(model[1].modules()) + list(model[3].modules())

    def _check_managed_modules(
        self,
        managed_modules: List[nn.Module],
        expected_managed_modules: List[nn.Module],
    ):
        self.assertEqual(len(managed_modules), len(expected_managed_modules))
        # Check set comparison since we do not require anything about the order
        self.assertEqual(set(managed_modules), set(expected_managed_modules))

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_managed_states_shared_params_and_buffers(self):
        model = nn.Sequential(*[MLP(8, with_buffer=True) for _ in range(3)])
        model[0].in_proj.weight = model[1].in_proj.weight
        model[2].in_proj.weight = model[1].in_proj.weight
        model[1].buffer = model[2].buffer
        # Assume calling `fully_shard` on `model`
        managed_modules = _get_managed_modules((model,))
        params, buffers = _get_managed_states(managed_modules)
        expected_params = list(model.parameters())  # de-dups shared
        expected_buffers = list(model.buffers())  # de-dups shared
        self._check_managed_states(params, buffers, expected_params, expected_buffers)

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_managed_states_nested_fully_shard(self):
        model = nn.Sequential(*[MLP(8, with_buffer=True) for _ in range(2)])
        fully_shard(model[0])
        # Assume calling `fully_shard` on `model`
        managed_modules = _get_managed_modules((model,))
        params, buffers = _get_managed_states(managed_modules)
        expected_params = list(model[1].parameters())
        expected_buffers = list(model[1].buffers())
        self._check_managed_states(params, buffers, expected_params, expected_buffers)

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_managed_states_list_of_mlps(self):
        model = nn.Sequential(*[MLP(8, with_buffer=True) for _ in range(5)])
        # Assume calling `fully_shard` on `[model[0], model[1], model[2]]`
        managed_modules = _get_managed_modules((model[0], model[1], model[2]))
        params, buffers = _get_managed_states(managed_modules)
        expected_params = (
            list(model[0].parameters())
            + list(model[1].parameters())
            + list(model[2].parameters())
        )
        expected_buffers = (
            list(model[0].buffers())
            + list(model[1].buffers())
            + list(model[2].buffers())
        )
        self._check_managed_states(params, buffers, expected_params, expected_buffers)

    def _check_managed_states(
        self,
        managed_params: List[nn.Parameter],
        managed_buffers: List[torch.Tensor],
        expected_managed_params: List[nn.Parameter],
        expected_managed_buffers: List[torch.Tensor],
    ):
        self.assertEqual(len(managed_params), len(expected_managed_params))
        self.assertEqual(len(managed_buffers), len(expected_managed_buffers))
        self.assertEqual(set(managed_params), set(expected_managed_params))
        self.assertEqual(set(managed_buffers), set(expected_managed_buffers))


class TestFullyShardParamModuleInfos(FSDPTestMultiThread):
    @property
    def world_size(self) -> int:
        return 2

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_get_param_module_infos_shared_params(self):
        model = nn.Sequential(*[MLP(8) for _ in range(2)])
        model[0].in_proj.weight = model[1].in_proj.weight
        managed_modules = _get_managed_modules((model,))
        params, _ = _get_managed_states(managed_modules)
        param_module_infos = _get_param_module_infos(params, model)
        self.assertEqual(len(param_module_infos), len(params))
        # We expect `params` to already have de-duplicated shared parameters
        expected_param_module_infos = [
            ParamModuleInfo(model[0].in_proj, "weight", [model[1].in_proj], ["weight"]),
            ParamModuleInfo(model[0].in_proj, "bias", [], []),
            ParamModuleInfo(model[0].out_proj, "weight", [], []),
            ParamModuleInfo(model[0].out_proj, "bias", [], []),
            ParamModuleInfo(model[1].in_proj, "bias", [], []),
            ParamModuleInfo(model[1].out_proj, "weight", [], []),
            ParamModuleInfo(model[1].out_proj, "bias", [], []),
        ]
        self.assertEqual(len(param_module_infos), len(expected_param_module_infos))
        self.assertEqual(param_module_infos, expected_param_module_infos)

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_get_param_module_infos_duplicates(self):
        mlp = MLP(8)
        model = nn.Sequential(mlp, mlp)  # shared MLP
        params = list(model.parameters())
        param_module_infos = _get_param_module_infos(params, model)
        self.assertEqual(len(param_module_infos), len(params))
        expected_param_module_infos = [
            ParamModuleInfo(mlp.in_proj, "weight", [mlp.in_proj], ["weight"]),
            ParamModuleInfo(mlp.in_proj, "bias", [mlp.in_proj], ["bias"]),
            ParamModuleInfo(mlp.out_proj, "weight", [mlp.out_proj], ["weight"]),
            ParamModuleInfo(mlp.out_proj, "bias", [mlp.out_proj], ["bias"]),
        ]
        self.assertEqual(len(param_module_infos), len(expected_param_module_infos))
        self.assertEqual(param_module_infos, expected_param_module_infos)

        model = nn.Sequential(*[MLP(8) for _ in range(2)])
        model[0].in_proj = model[1].in_proj  # shared in-projection
        params = list(model.parameters())
        param_module_infos = _get_param_module_infos(params, model)
        self.assertEqual(len(param_module_infos), len(params))
        expected_param_module_infos = [
            ParamModuleInfo(model[0].in_proj, "weight", [model[1].in_proj], ["weight"]),
            ParamModuleInfo(mlp.in_proj, "bias", [], []),
            ParamModuleInfo(mlp.out_proj, "weight", [], []),
            ParamModuleInfo(mlp.out_proj, "bias", [], []),
        ]

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_get_param_module_infos_list_of_mlps(self):
        model = nn.Sequential(*[MLP(8) for _ in range(2)])
        managed_modules = _get_managed_modules((model[0], model[1]))
        params, _ = _get_managed_states(managed_modules)
        param_module_infos = _get_param_module_infos(params, model)
        self.assertEqual(len(param_module_infos), len(params))
        expected_param_module_infos = [
            ParamModuleInfo(model[0].in_proj, "weight", [], []),
            ParamModuleInfo(model[0].in_proj, "bias", [], []),
            ParamModuleInfo(model[0].out_proj, "weight", [], []),
            ParamModuleInfo(model[0].out_proj, "bias", [], []),
            ParamModuleInfo(model[1].in_proj, "weight", [], []),
            ParamModuleInfo(model[1].in_proj, "bias", [], []),
            ParamModuleInfo(model[1].out_proj, "weight", [], []),
            ParamModuleInfo(model[1].out_proj, "bias", [], []),
        ]
        self.assertEqual(len(param_module_infos), len(expected_param_module_infos))
        self.assertEqual(param_module_infos, expected_param_module_infos)


class TestFullyShardShardedParameterTensor(FSDPTestMultiThread):
    @property
    def world_size(self) -> int:
        return 2

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_shard_tensor_parameters(self):
        # Use odd dim sizes to test uneven shards
        model = nn.Sequential(*[MLP(3, dim_multiplier=3) for _ in range(3)])
        orig_params = [param.detach().clone() for param in model.parameters()]
        fully_shard(model)
        sharded_params = list(model.parameters())
        self._check_1d_sharded_parameters(orig_params, sharded_params)

        model = nn.Sequential(*[MLP(3, dim_multiplier=3) for _ in range(3)])
        model[0].in_proj = model[1].in_proj
        orig_params = [param.detach().clone() for param in model.parameters()]
        fully_shard(model)
        sharded_params = list(model.parameters())
        self._check_1d_sharded_parameters(orig_params, sharded_params)

    def _check_1d_sharded_parameters(
        self, orig_params: List[nn.Parameter], sharded_params: List[nn.Parameter]
    ):
        self.assertEqual(len(orig_params), len(sharded_params))
        global_mesh = init_device_mesh("cuda", (self.world_size,))
        for orig_param, sharded_param in zip(orig_params, sharded_params):
            self.assertIsInstance(sharded_param, DTensor)
            self.assertEqual(sharded_param.device_mesh, global_mesh)
            self.assertEqual(sharded_param.size(), orig_param.size())
            self.assertEqual(sharded_param.stride(), orig_param.stride())
            self.assertEqual(sharded_param._spec.placements, (Shard(0),))
            chunks = torch.chunk(orig_param, self.world_size, dim=0)
            self.assertEqual(sharded_param._local_tensor, chunks[self.rank])

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_raise_scalar_parameter(self):
        """Tests raising an exception when the model has scalar parameters."""
        model = nn.Sequential(*[MLP(3, dim_multiplier=3) for _ in range(3)])
        model.register_parameter("scalar_p", nn.Parameter(torch.tensor(1.0).cuda()))
        with self.assertRaisesRegex(
            ValueError, "Change scalar_p to a 1D tensor with numel equals to 1."
        ):
            fully_shard(model)


class TestFullyShardShardedParameterDTensor(FSDPTestMultiThread):
    @property
    def world_size(self) -> int:
        return 4

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_shard_dtensor_parameters(self):
        dp_size = 2 if self.world_size > 2 else 1
        global_mesh = init_device_mesh(
            "cuda", (dp_size, self.world_size // dp_size), mesh_dim_names=("dp", "tp")
        )
        dp_mesh, tp_mesh = global_mesh["dp"], global_mesh["tp"]
        # Use odd dim sizes to test uneven shards
        # TODO: change "mlp_dim" back to 9 when uneven sharding
        # is supported for FSDP+TP
        model = MLP(8, dim_multiplier=3)
        orig_params = [param.detach().clone() for param in model.parameters()]
        orig_param_names = [param_name for param_name, _ in model.named_parameters()]
        parallelize_module(
            model,
            tp_mesh,
            {"in_proj": ColwiseParallel(), "out_proj": RowwiseParallel()},
        )
        fully_shard(model, mesh=dp_mesh)
        sharded_params = list(model.parameters())
        self.assertEqual(len(orig_params), len(sharded_params))
        for orig_param_name, orig_param, sharded_param in zip(
            orig_param_names, orig_params, sharded_params
        ):
            self.assertIsInstance(sharded_param, DTensor)
            self.assertEqual(sharded_param.device_mesh, global_mesh)
            self.assertEqual(sharded_param.size(), orig_param.size())
            self.assertEqual(sharded_param.stride(), orig_param.stride())
            if "in_proj" in orig_param_name:
                expected_placements = (
                    _StridedShard(0, split_factor=tp_mesh.size()),
                    Shard(0),
                )
            elif "out_proj" in orig_param_name and "weight" in orig_param_name:
                expected_placements = (Shard(0), Shard(1))
            else:
                expected_placements = (Shard(0), Replicate())
            self.assertEqual(sharded_param._spec.placements, expected_placements)


class TestFullyShardLazyInit(FSDPTestMultiThread):
    @property
    def world_size(self) -> int:
        return 2

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_fully_shard_is_root(self):
        """
        Tests that ``_is_root`` is set correctly after lazy initialization.

        FSDP(model(
            0: MLP(FSDP(in_proj), FSDP(out_proj)),
            1: MLP(in_proj, out_proj),
        ))
        """
        model = nn.Sequential(MLP(8), MLP(8))
        fully_shard(model[0].in_proj)
        fully_shard(model[0].out_proj)
        fully_shard(model)  # root gets `model[1]`
        root_state = fully_shard.state(model)
        root_state._lazy_init()

        model0_in_proj_state = fully_shard.state(model[0].in_proj)
        model0_out_proj_state = fully_shard.state(model[0].out_proj)
        self.assertTrue(root_state._is_root)
        self.assertFalse(model0_in_proj_state._is_root)
        self.assertFalse(model0_out_proj_state._is_root)

        all_states = root_state._state_ctx.all_states
        self.assertEqual(len(all_states), 3)
        self.assertEqual(
            all_states, [root_state, model0_in_proj_state, model0_out_proj_state]
        )

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_fully_shard_module_and_param_fqns(self):
        """
        Tests that the module and parameter FQNs are computed correctly after
        lazy initialization.

        FSDP(model(
            0: MLP(FSDP(in_proj), FSDP(out_proj)),
            1: MLP(in_proj, out_proj),
        ))
        """
        model = nn.Sequential(MLP(8), MLP(8))
        fully_shard(model[0].in_proj)
        fully_shard(model[0].out_proj)
        fully_shard(model)  # root gets `model[1]`
        root_state = fully_shard.state(model)
        root_state._lazy_init()

        root_param_group = root_state._fsdp_param_group
        self.assertIsNotNone(root_param_group)
        self.assertEqual(root_param_group._module_fqn, "")
        root_param_fqns = {
            fsdp_param._param_fqn for fsdp_param in root_param_group.fsdp_params
        }
        self.assertEqual(
            root_param_fqns,
            {
                "1.in_proj.weight",
                "1.in_proj.bias",
                "1.out_proj.weight",
                "1.out_proj.bias",
            },
        )

        model0_in_proj_state = fully_shard.state(model[0].in_proj)
        model0_in_proj_param_group = model0_in_proj_state._fsdp_param_group
        self.assertIsNotNone(model0_in_proj_param_group)
        self.assertEqual(model0_in_proj_param_group._module_fqn, "0.in_proj")
        model0_in_proj_param_fqns = {
            fsdp_param._param_fqn
            for fsdp_param in model0_in_proj_param_group.fsdp_params
        }
        self.assertEqual(
            model0_in_proj_param_fqns, {"0.in_proj.weight", "0.in_proj.bias"}
        )

        model0_out_proj_state = fully_shard.state(model[0].out_proj)
        model0_out_proj_param_group = model0_out_proj_state._fsdp_param_group
        self.assertIsNotNone(model0_out_proj_param_group)
        self.assertEqual(model0_out_proj_param_group._module_fqn, "0.out_proj")
        model0_out_proj_param_fqns = {
            fsdp_param._param_fqn
            for fsdp_param in model0_out_proj_param_group.fsdp_params
        }
        self.assertEqual(
            model0_out_proj_param_fqns, {"0.out_proj.weight", "0.out_proj.bias"}
        )

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_fully_shard_double_lazy_init(self):
        model = nn.Sequential(MLP(8), MLP(8))
        fully_shard(model[0].in_proj)
        fully_shard(model[0].out_proj)
        fully_shard(model)
        root_state = fully_shard.state(model)
        model0_in_proj_state = fully_shard.state(model[0].in_proj)
        model0_in_proj_state._lazy_init()
        regex = (
            "FSDP state has already been lazily initialized for 0.in_proj\n"
            "FSDP requires running forward through the root module first"
        )
        with self.assertRaisesRegex(RuntimeError, regex):
            root_state._lazy_init()

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_fully_shard_multi_module_root(self):
        model = nn.Sequential(MLP(8), MLP(8))
        fully_shard([model[0], model[1]])
        root_state = fully_shard.state(model[0])
        regex = "FSDP requires a single root module but got "
        with self.assertRaisesRegex(RuntimeError, regex):
            root_state._lazy_init()


class TestFullyShardMetaDeviceInit(FSDPTestMultiThread):
    @property
    def world_size(self) -> int:
        return 4

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_meta_device_1d_init(self):
        default_pg = torch.distributed.distributed_c10d._get_default_group()
        mesh = init_device_mesh("cuda", mesh_shape=(default_pg.size(),))

        # Test both even sharding (8) and uneven sharding (3)
        for mlp_dim in (8, 3):
            with torch.device("meta"):
                model = nn.Sequential(MLP(mlp_dim, with_buffer=True), MLP(mlp_dim))
                for param in model.parameters():
                    self.assertEqual(param.device, torch.device("meta"))
                fully_shard(model[0], mesh=mesh)
                fully_shard(model[1], mesh=mesh)
                fully_shard(model, mesh=mesh)
            for param in model.parameters():
                self.assertEqual(param.device, torch.device("meta"))
            self._test_to_empty_and_reset_parameters(model, mesh, mlp_dim)

        # Test that we can call `fully_shard` under meta-device context and
        # that `init_device_mesh` call still works
        mlp_dim = 8
        with torch.device("meta"):
            model = nn.Sequential(MLP(mlp_dim, with_buffer=True), MLP(mlp_dim))
            for param in model.parameters():
                self.assertEqual(param.device, torch.device("meta"))
            for module in (model[0], model[1], model):
                fully_shard(module)
        for param in model.parameters():
            self.assertEqual(param.device, torch.device("meta"))
        self._test_to_empty_and_reset_parameters(model, mesh, mlp_dim)

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_meta_device_2d_init(self):
        assert self.world_size >= 4, f"{self.world_size}"
        dp_size = 2
        global_mesh = init_device_mesh(
            "cuda", (dp_size, self.world_size // dp_size), mesh_dim_names=("dp", "tp")
        )
        dp_mesh, tp_mesh = global_mesh["dp"], global_mesh["tp"]

        # Test both even sharding (8) and uneven sharding (3)
        for mlp_dim in (8, 3):
            with torch.device("meta"):
                model = MLP(mlp_dim, with_buffer=True)
                for param in model.parameters():
                    self.assertEqual(param.device, torch.device("meta"))
                parallelize_module(
                    model,
                    tp_mesh,
                    {"in_proj": ColwiseParallel(), "out_proj": RowwiseParallel()},
                )
                for param in model.parameters():
                    self.assertEqual(param.device, torch.device("meta"))
                fully_shard(model.in_proj, mesh=dp_mesh)
                fully_shard(model.out_proj, mesh=dp_mesh)
                fully_shard(model, mesh=dp_mesh)
            for param in model.parameters():
                self.assertEqual(param.device, torch.device("meta"))
            self._test_to_empty_and_reset_parameters(model, global_mesh, mlp_dim)

    def _test_to_empty_and_reset_parameters(
        self, model: nn.Module, mesh: DeviceMesh, mlp_dim: int
    ):
        # Check that we can materialize it on GPU with empty values
        device = torch.device("cuda", torch.cuda.current_device())
        model.to_empty(device=device)
        for param in model.parameters():
            self.assertEqual(param.device, device)
        optim = torch.optim.Adam(model.parameters(), lr=1e-2)

        # Check that `reset_parameters()` on each module initializes values
        const = 1337
        for tensor in itertools.chain(model.parameters(), model.buffers()):
            tensor.detach().fill_(const)
        for module in model.modules():
            if hasattr(module, "reset_parameters"):
                module.reset_parameters()
        for param in model.parameters():
            local_tensor = param.to_local()
            if local_tensor.numel() > 0:
                self.assertNotEqual(local_tensor, torch.ones_like(local_tensor) * const)
        for buffer in model.buffers():
            self.assertNotEqual(buffer, torch.ones_like(buffer) * const)

        # Check that we can run an iteration without erroring
        inp = torch.randn((4, mlp_dim), device="cuda")
        model(inp).sum().backward()
        optim.step()

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_invalid_meta_device_init(self):
        default_pg = torch.distributed.distributed_c10d._get_default_group()
        mesh = init_device_mesh("cuda", mesh_shape=(default_pg.size(),))
        mlp_dim = 8
        with torch.device("meta"):
            model = nn.Sequential(MLP(mlp_dim, with_buffer=True), MLP(mlp_dim))
            for param in model.parameters():
                self.assertEqual(param.device, torch.device("meta"))
            fully_shard(model[0], mesh=mesh)
            fully_shard(model[1], mesh=mesh)
            fully_shard(model, mesh=mesh)
        inp = torch.randn((4, mlp_dim), device="cuda")
        error_regex = (
            "FSDP parameters should be materialized from meta device before training, "
            "but the following were still on meta device: "
            r"\['0.in_proj.weight', '0.in_proj.bias', '0.out_proj.weight', '0.out_proj.bias'\]"
        )
        with self.assertRaisesRegex(RuntimeError, error_regex):
            model(inp)

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_rank0_broadcast_meta_device_init(self):
        model_args = ModelArgs(dropout_p=0.0)
        # Assume we have a CPU full state dict on rank 0
        if self.rank == 0:
            torch.manual_seed(42)
            ref_model = Transformer(model_args)
            full_sd = ref_model.state_dict()
            for param in full_sd.values():
                self.assertEqual(param.device, torch.device("cpu"))

        # Initialize the sharded model on meta device
        fsdp_mesh = init_device_mesh("cuda", (self.world_size,))
        with torch.device("meta"):
            model = Transformer(model_args)
        for module in model.modules():
            if isinstance(module, TransformerBlock):
                fully_shard(module, mesh=fsdp_mesh)
        fully_shard(model, mesh=fsdp_mesh)
        for param in model.parameters():
            self.assertEqual(param.device, torch.device("meta"))

        # Construct a sharded state dict from the rank 0 full state dict by
        # broadcasting and sharding
        meta_sharded_sd = model.state_dict()
        sharded_sd = {}
        if self.rank == 0:
            self.assertEqual(len(meta_sharded_sd), len(full_sd))
            self.assertEqual(list(meta_sharded_sd.keys()), list(full_sd.keys()))
            for (param_name, full_param), sharded_meta_param in zip(
                full_sd.items(), meta_sharded_sd.values()
            ):
                full_param = full_param.detach().cuda()
                mesh = sharded_meta_param.device_mesh
                dist.broadcast(full_param, src=0, group=mesh.get_group(0))
                sharded_tensor = distribute_tensor(
                    full_param, mesh, sharded_meta_param.placements
                )
                sharded_sd[param_name] = nn.Parameter(sharded_tensor)
        else:
            for param_name, sharded_meta_param in meta_sharded_sd.items():
                full_tensor = torch.empty(
                    sharded_meta_param.size(),
                    device="cuda",
                    dtype=sharded_meta_param.dtype,
                )
                mesh = sharded_meta_param.device_mesh
                dist.broadcast(full_tensor, src=0, group=mesh.get_group(0))
                sharded_tensor = distribute_tensor(
                    full_tensor, mesh, sharded_meta_param.placements
                )
                sharded_sd[param_name] = nn.Parameter(sharded_tensor)

        model.load_state_dict(sharded_sd, assign=True)
        for param in model.parameters():
            self.assertIsInstance(param, DTensor)
            self.assertEqual(param.device.type, "cuda")

        # Construct the reference model on nonzero ranks by broadcasting the
        # unsharded model from rank 0 and sharding on all ranks
        if self.rank != 0:
            ref_model = Transformer(model_args)
        for param in ref_model.parameters():
            torch.distributed.broadcast(param.detach(), src=0)
        for module in ref_model.modules():
            if isinstance(module, TransformerBlock):
                fully_shard(module, mesh=fsdp_mesh)
        fully_shard(ref_model, mesh=fsdp_mesh)

        for (param_name, param), (ref_param_name, ref_param) in zip(
            model.named_parameters(), ref_model.named_parameters()
        ):
            self.assertEqual(param_name, ref_param_name)
            self.assertEqual(param, ref_param)

        # Check one forward/backward for parity
        inp = torch.randint(0, model_args.vocab_size, (2, 16), device="cuda")
        loss = model(inp).sum()
        loss.backward()
        ref_loss = ref_model(inp).sum()
        ref_loss.backward()
        self.assertEqual(loss, ref_loss)
        for param, ref_param in zip(model.parameters(), ref_model.parameters()):
            self.assertEqual(param.grad, ref_param.grad)


class TestFullyShardProcessGroupInit(FSDPTestMultiThread):
    @property
    def world_size(self) -> int:
        return 4

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_1d_process_group_init(self):
        assert self.world_size == 4, f"{self.world_size}"
        # For convenience, use device mesh's infra to construct the DP PG
        # (in practice, the trainer would do it manually via `new_group()`)
        dp_size = 2
        global_mesh = init_device_mesh(
            "cuda", (dp_size, self.world_size // dp_size), mesh_dim_names=("dp", "tp")
        )
        ref_dp_mesh, tp_mesh = global_mesh["dp"], global_mesh["tp"]
        dp_pg = ref_dp_mesh.get_group(0)

        # Check the `from_group()` API for correctness
        dp_mesh = DeviceMesh.from_group(dp_pg, "cuda", mesh_dim_names=("dp",))
        # Only compare the mesh tensors, not `DeviceMesh` objects themselves,
        # since the ref has a parent mesh, while the `from_group` one does not
        self.assertEqual(dp_mesh.mesh, ref_dp_mesh.mesh)
        self.assertEqual(dp_mesh._coordinate_on_dim, ref_dp_mesh._coordinate_on_dim)
        self.assertEqual(dp_mesh._dim_group_infos, ref_dp_mesh._dim_group_infos)

        # Check 1D FSDP forward/backward parity over the DP mesh
        # NOTE: We cannot use 2D DTensor-based training here because the DP
        # mesh from `from_group` does not respect the parent mesh.
        torch.manual_seed(42)
        mlp_dim = 8
        ref_model = MLP(mlp_dim)
        for param in ref_model.parameters():
            dist.broadcast(param.detach(), src=0)
        model = copy.deepcopy(ref_model)

        # Parallelize the test model with the ref DP mesh
        for module in (ref_model.in_proj, ref_model.out_proj, ref_model):
            fully_shard(module, mesh=ref_dp_mesh)
        # Parallelize the test model with the new DP mesh from the PG
        for module in (model.in_proj, model.out_proj, model):
            fully_shard(module, mesh=dp_mesh)

        # Ensure that TP ranks have the same input
        inp = torch.randn((4, mlp_dim), device="cuda")
        if self.rank in (0, 1):
            dist.broadcast(inp, src=0, group=tp_mesh.get_group(0))
        elif self.rank in (2, 3):
            dist.broadcast(inp, src=2, group=tp_mesh.get_group(0))

        ref_loss = ref_model(inp).sum()
        ref_loss.backward()
        loss = model(inp).sum()
        loss.backward()
        self.assertEqual(loss, ref_loss)
        for param, ref_param in zip(model.parameters(), ref_model.parameters()):
            # Cannot compare `DTensor`s directly since their meshes are not
            # equal due to the ref parameter's mesh having a parent mesh while
            # the other's mesh does not
            self.assertEqual(param.to_local(), ref_param.to_local())
            self.assertEqual(param.device_mesh.mesh, ref_param.device_mesh.mesh)
            self.assertEqual(param.grad.to_local(), ref_param.grad.to_local())
            self.assertEqual(
                param.grad.device_mesh.mesh, ref_param.grad.device_mesh.mesh
            )

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_2d_process_group_init(self):
        shard_mesh_dim_size = 2
        assert (
            self.world_size % shard_mesh_dim_size == 0
        ), f"Expects {self.world_size} to be divisible by {shard_mesh_dim_size}"
        replicate_mesh_dim_size = self.world_size // shard_mesh_dim_size
        mesh_dim_names = ("replicate", "shard")
        ref_mesh = init_device_mesh(
            "cuda",
            (replicate_mesh_dim_size, shard_mesh_dim_size),
            mesh_dim_names=mesh_dim_names,
        )

        # Use the global PG as the parent group (in practice, this could be a
        # subgroup of the global PG)
        dp_group = dist.distributed_c10d._get_default_group()
        dp_shard_group = _init_intra_node_process_group(shard_mesh_dim_size)
        dp_replicate_group = _init_inter_node_process_group(
            dp_group, replicate_mesh_dim_size
        )
        mesh_tensor = torch.tensor(
            dist.get_process_group_ranks(dp_group), dtype=torch.int
        ).view(replicate_mesh_dim_size, shard_mesh_dim_size)

        # Check the `from_group()` API for correctness
        mesh = DeviceMesh.from_group(
            [dp_replicate_group, dp_shard_group],
            "cuda",
            mesh_dim_names=mesh_dim_names,
            mesh=mesh_tensor,
        )
        self.assertEqual(mesh.mesh, ref_mesh.mesh)
        self.assertEqual(mesh._coordinate_on_dim, ref_mesh._coordinate_on_dim)
        for (tag, ranks, group_name), (ref_tag, ref_ranks, ref_group_name) in zip(
            mesh._dim_group_infos, ref_mesh._dim_group_infos
        ):
            # Since we manually constructed new subgroups, the test and ref
            # groups are not the same
            self.assertEqual(ranks, ref_ranks)
        for mesh_dim_name in mesh_dim_names:
            child_mesh = mesh[mesh_dim_name]
            ref_child_mesh = ref_mesh[mesh_dim_name]
            self.assertEqual(child_mesh, ref_child_mesh)
            child_ranks = dist.distributed_c10d.get_process_group_ranks(
                child_mesh.get_group()
            )
            ref_child_ranks = dist.distributed_c10d.get_process_group_ranks(
                ref_child_mesh.get_group()
            )
            self.assertEqual(child_ranks, ref_child_ranks)

        # Check HSDP forward/backward parity
        torch.manual_seed(42)
        mlp_dim = 8
        ref_model = MLP(mlp_dim)
        for param in ref_model.parameters():
            dist.broadcast(param.detach(), src=0)
        model = copy.deepcopy(ref_model)

        # Parallelize the test model with the ref mesh
        for module in (ref_model.in_proj, ref_model.out_proj, ref_model):
            fully_shard(module, mesh=ref_mesh)
        # Parallelize the test model with the new mesh from the PG
        for module in (model.in_proj, model.out_proj, model):
            fully_shard(module, mesh=mesh)

        inp = torch.randn((4, mlp_dim), device="cuda")
        ref_loss = ref_model(inp).sum()
        ref_loss.backward()
        loss = model(inp).sum()
        loss.backward()
        self.assertEqual(loss, ref_loss)
        for param, ref_param in zip(model.parameters(), ref_model.parameters()):
            self.assertEqual(param, ref_param)
            self.assertEqual(param.grad, ref_param.grad)


class TestFullyShardHSDPBroadcast(FSDPTestMultiThread):
    @property
    def world_size(self) -> int:
        return 4

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_hsdp_broadcast_across_replicas(self):
        shard_size, replicate_size = 2, 2
        mesh = init_device_mesh(
            "cuda", (replicate_size, shard_size), mesh_dim_names=("replicate", "shard")
        )
        model_args = ModelArgs()
        model = Transformer(model_args)
        # Add a buffer to show that this flow works for buffers too
        model.buf = torch.nn.Buffer(torch.randn((model_args.dim,)))
        for module in model.modules():
            if isinstance(module, TransformerBlock):
                fully_shard(module, mesh=mesh)
        fully_shard(model, mesh=mesh)

        # Only preserve the model states on the replicate mesh's rank 0
        if mesh.get_local_rank("replicate") > 0:
            for tensor in itertools.chain(model.parameters(), model.buffers()):
                tensor.detach().fill_(1337)

        # Check that replicas are different
        for tensor in itertools.chain(model.parameters(), model.buffers()):
            local_tensor = tensor.to_local() if isinstance(tensor, DTensor) else tensor
            local_tensor_list = [
                torch.empty_like(local_tensor) for _ in range(mesh["replicate"].size())
            ]
            dist.all_gather(
                local_tensor_list, local_tensor, group=mesh.get_group("replicate")
            )
            for other_local_tensor in local_tensor_list[1:]:
                self.assertEqual(other_local_tensor.shape, local_tensor_list[0].shape)
                self.assertNotEqual(other_local_tensor, local_tensor_list[0])

        # Broadcast from replicate mesh's rank 0
        replicate_group = mesh.get_group("replicate")
        for tensor in itertools.chain(model.parameters(), model.buffers()):
            # E.g. for mesh [[0, 1, 2, 3], [4, 5, 6, 7]] sharding on dim-1 and
            # replicating on dim-0, broadcast with sources 0, 1, 2, 3
            src_rank = dist.get_process_group_ranks(replicate_group)[0]
            torch.distributed.broadcast(
                tensor.to_local() if isinstance(tensor, DTensor) else tensor,
                src=src_rank,
                group=replicate_group,
            )

        # Check that replicas are the same
        for tensor in itertools.chain(model.parameters(), model.buffers()):
            local_tensor = tensor.to_local() if isinstance(tensor, DTensor) else tensor
            local_tensor_list = [
                torch.empty_like(local_tensor) for _ in range(mesh["replicate"].size())
            ]
            dist.all_gather(
                local_tensor_list, local_tensor, group=mesh.get_group("replicate")
            )
            for other_local_tensor in local_tensor_list[1:]:
                self.assertEqual(other_local_tensor, local_tensor_list[0])

        # Check that we can run an iteration without erroring
        inp = torch.randint(0, model_args.vocab_size, (2, 16), device="cuda")
        model(inp).sum().backward()


if __name__ == "__main__":
    run_tests()
