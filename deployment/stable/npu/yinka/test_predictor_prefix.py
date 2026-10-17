"""Host-side tests of the exact patched upstream methods, without importing vLLM.

Set YINKA_PREDICTOR_SOURCE to the patched qwen3_code_predictor.py. Real NPU
streaming/precision comparisons are still required; these test bucket dispatch.
"""
import ast
from contextlib import nullcontext
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


@unittest.skipUnless(os.getenv("YINKA_PREDICTOR_SOURCE"), "Requires the pinned, patched upstream source")
class PredictorPrefixTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse(Path(os.environ["YINKA_PREDICTOR_SOURCE"]).read_text())
        self.wrapper = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                            and n.name == "CodePredictorWrapper")

    def test_npu_is_separately_opt_in(self):
        constructor = next(n for n in self.wrapper.body if isinstance(n, ast.FunctionDef)
                           and n.name == "__init__")
        assignment = next(n for n in ast.walk(constructor) if isinstance(n, ast.Assign)
                          and any(isinstance(t, ast.Attribute) and t.attr == "_prefix_graphs_enabled"
                                  for t in n.targets))
        expression = compile(ast.Expression(assignment.value), "predictor-flag", "eval")
        for npu in (False, True):
            for cuda_flag in (False, True):
                for npu_flag in (False, True):
                    for graphs in (False, True):
                        actual = eval(expression, {
                            "is_npu": npu, "prefix_graphs_requested": cuda_flag,
                            "npu_prefix_graphs_requested": npu_flag,
                            "wrapper_config": SimpleNamespace(use_cuda_graphs=graphs),
                        })
                        self.assertEqual(actual, graphs and (npu_flag if npu else cuda_flag))

    def test_capture_matches_warmed_prefixes_and_full_fallback(self):
        method = next(n for n in self.wrapper.body if isinstance(n, ast.FunctionDef)
                      and n.name == "_capture_npu_graphs")
        module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
        for shapes in ({1: (1, 17), 4: (4, 17)},
                       {(1, 2): (1, 2), (1, 16): (1, 16), 4: (4, 17)}):
            captured = []

            class Buffer:
                def __getitem__(self, index):
                    return (index[0].stop, index[1].stop)

            def forward(buffer_shape, positions):
                self.assertEqual(buffer_shape, positions.shape)
                captured.append(buffer_shape)
                return buffer_shape

            namespace = {
                "torch": SimpleNamespace(npu=SimpleNamespace(
                    graph_pool_handle=lambda: "pool", NPUGraph=object,
                    graph=lambda *args, **kwargs: nullcontext(),
                )), "logger": Mock(),
            }
            exec(compile(module, "predictor-capture", "exec"), namespace)
            wrapper = SimpleNamespace(
                _proj_buf=Buffer(), _bucket_pos_ids={k: SimpleNamespace(shape=v) for k, v in shapes.items()},
                _device_graphs={}, _compiled_model_fwd=forward,
            )
            namespace["_capture_npu_graphs"](wrapper)
            self.assertEqual(set(wrapper._device_graphs), set(shapes))
            self.assertEqual(captured, list(shapes.values()))


if __name__ == "__main__":
    unittest.main()
