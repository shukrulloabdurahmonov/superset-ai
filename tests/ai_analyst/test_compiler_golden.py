"""Golden-file regression for the ai_analyst compiler.

The compiler was ported from a standalone build.py whose bundles were
import-tested and render-verified against a real Superset instance. These
tests pin the port to that proven output: compiling the fixture specs must
reproduce the fixture bundles exactly (ignoring the metadata timestamp).

Runs without a Superset app context (the compiler is dependency-free), so the
module is loaded by file path to avoid importing the superset package.
"""
import importlib.util
import io
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).parent
FIXTURES = HERE / "fixtures"
CORE = HERE.parents[1] / "superset" / "ai_analyst" / "compiler" / "core.py"

_spec = importlib.util.spec_from_file_location("ai_compiler_core", CORE)
core = importlib.util.module_from_spec(_spec)
sys.modules["ai_compiler_core"] = core
_spec.loader.exec_module(core)


@pytest.fixture()
def compiler():
    return core.Compiler(
        database=core.DatabaseRef(
            name="Trino",
            uuid="95c6a73b-0cce-44f3-a132-1a70736e496e",
            yaml_text=(FIXTURES / "database.yaml").read_text(),
        ),
        # namespace the golden bundles were generated with
        namespace="superset.dashboards.tteampro",
        default_catalog="iceberg",
    )


@pytest.mark.parametrize(
    "spec_file,golden_zip",
    [
        ("active_users.yaml", "dau-codegen_bundle.zip"),
        ("competitors.yaml", "competitors_bundle.zip"),
    ],
)
def test_bundle_matches_golden(compiler, spec_file, golden_zip):
    spec = yaml.safe_load((FIXTURES / spec_file).read_text())
    _, blob = compiler.compile(spec)
    new = zipfile.ZipFile(io.BytesIO(blob))
    old = zipfile.ZipFile(FIXTURES / golden_zip)

    assert set(new.namelist()) == set(old.namelist())
    for path in sorted(new.namelist()):
        a = yaml.safe_load(new.read(path))
        b = yaml.safe_load(old.read(path))
        if path.endswith("metadata.yaml"):
            a.pop("timestamp"), b.pop("timestamp")
        assert a == b, f"{path} differs from golden"


def test_unsupported_viz_type_hard_errors(compiler):
    spec = yaml.safe_load((FIXTURES / "active_users.yaml").read_text())
    next(iter(spec["charts"].values()))["type"] = "sankey"
    with pytest.raises(core.SpecError, match="unsupported type"):
        compiler.compile(spec)


def test_cal_heatmap_requires_bounded_time_range(compiler):
    spec = yaml.safe_load((FIXTURES / "active_users.yaml").read_text())
    spec["charts"]["heatmap"]["time_range"] = "No filter"
    with pytest.raises(core.SpecError, match="bounded"):
        compiler.compile(spec)


def test_missing_required_key_errors(compiler):
    with pytest.raises(core.SpecError, match="missing required key"):
        compiler.compile({"title": "x", "slug": "x", "datasets": {}, "charts": {}})


def test_content_versioned_uuids_change_on_edit(compiler):
    spec = yaml.safe_load((FIXTURES / "active_users.yaml").read_text())

    def chart_uuids(blob):
        z = zipfile.ZipFile(io.BytesIO(blob))
        return {
            p: yaml.safe_load(z.read(p))["uuid"]
            for p in z.namelist()
            if "/charts/" in p
        }

    _, before = compiler.compile(spec)
    spec["charts"]["total_dau"]["row_limit"] = 555
    _, after = compiler.compile(spec)
    a, b = chart_uuids(before), chart_uuids(after)
    changed = [p for p in a if a[p] != b[p]]
    assert len(changed) == 1  # only the edited chart re-versions
    assert "total_dau" in changed[0]
