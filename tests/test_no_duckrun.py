"""fabcontext must not depend on duckrun - by import, by name, or by accident.

This is a gate, not a formality. duckrun requires duckdb >= 1.5.4 and deltalake == 1.5.0
exactly, while the Fabric Python runtime ships 1.4.4 and 1.2.1, so a stray import would mean
pip replacing two native libraries and a kernel restart before anything could run - the whole
thing fabcontext exists to avoid. It is also installed on the machine this was written on,
which is exactly how such an import would go unnoticed.
"""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_importing_fabcontext_does_not_pull_in_duckrun():
    """Checked in a subprocess: the test session itself may have imported it for other
    reasons, and then `sys.modules` here would prove nothing."""
    code = ("import fabcontext, fabcontext.publish, fabcontext.graph, fabcontext.profiling, "
            "fabcontext.files, fabcontext.api, fabcontext.fetch, fabcontext.wiki, "
            "fabcontext.viz, sys; "
            "print(sorted(m for m in sys.modules if m.split('.')[0] in "
            "('duckrun', 'dbt', 'pyarrow', 'obstore')))")
    out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                         capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]", out.stdout


def test_no_module_mentions_duckrun():
    offenders = []
    for path in sorted(ROOT.joinpath("fabcontext").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), 1):
            if "duckrun" in line or "obstore" in line:
                offenders.append(str(path.relative_to(ROOT)) + ":" + str(number) + " " + line.strip())
    assert not offenders, offenders


def test_declared_dependencies_are_all_in_the_fabric_runtime():
    """Every declared dependency must already exist in the runtime list, or installing the
    package stops being free."""
    import tomllib

    with open(ROOT / "pyproject.toml", "rb") as handle:
        declared = tomllib.load(handle)["project"]["dependencies"]
    runtime = {line.split()[0].lower().replace("_", "-")
               for line in (ROOT / "docs" / "fabric-runtime.txt").read_text(
                   encoding="utf-8").splitlines()
               if line and not line.startswith(("#", "-", "Package", "Note"))}
    missing = [d for d in declared
               if d.split(">")[0].split("=")[0].split("<")[0].strip().lower() not in runtime]
    assert not missing, missing
