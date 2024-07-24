from pathlib import Path
from pytest import fixture


@fixture
def output_folder():
    folder = Path(__file__).parent / "test_output"
    folder.mkdir(parents=True, exist_ok=True)
    return folder
