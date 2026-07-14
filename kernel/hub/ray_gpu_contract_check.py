"""Real Ray 2.56 GPU scheduling contract without requiring physical accelerator hardware."""

from __future__ import annotations

import os
import tempfile


def main() -> None:
    os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")
    import pyarrow as pa
    import pyarrow.parquet as pq
    import ray

    ray.init(
        num_cpus=2,
        num_gpus=1,
        resources={"accelerator_type:H100": 1},
        include_dashboard=False,
        configure_logging=False,
        log_to_driver=False,
    )
    gpu_opts = {"num_gpus": 1.0, "accelerator_type": "H100"}

    @ray.remote(**gpu_opts)
    def healthy_typed_task() -> str:
        return ray.get_runtime_context().get_accelerator_ids().get("GPU", ["missing"])[0]

    assert ray.get(healthy_typed_task.remote(), timeout=10) != "missing"

    wrong = healthy_typed_task.options(accelerator_type="A100").remote()
    ready, _ = ray.wait([wrong], timeout=2)
    assert not ready, "an A100 task must remain pending on an H100-only node"
    ray.cancel(wrong, force=True)

    with tempfile.TemporaryDirectory(prefix="dp-ray-gpu-contract-") as tmp:
        source, output = os.path.join(tmp, "source.parquet"), os.path.join(tmp, "output")
        pq.write_table(pa.table({"id": list(range(8))}), source)
        dataset = ray.data.read_parquet(source, ray_remote_args=gpu_opts)
        mapped = dataset.map_batches(
            lambda table: table,
            batch_format="pyarrow",
            batch_size=2,
            **gpu_opts,
        )
        assert sorted(row["id"] for row in mapped.take_all()) == list(range(8))
        mapped.write_parquet(output, ray_remote_args=gpu_opts)
        assert ray.data.read_parquet(output).count() == 8

        try:
            dataset.map_batches(lambda table: table, batch_format="pyarrow", **gpu_opts)
        except ValueError as exc:
            assert "batch_size" in str(exc)
        else:
            raise AssertionError("Ray 2.56 must reject a GPU map without finite batch_size")

    print("[gpu-contract] PASS: typed affinity, wrong-GPU pending, finite map, typed read/write")


if __name__ == "__main__":
    main()
