"""Dataset category names must not split tracked instances into classes."""

import pytest

from app.main import _dataset_class_name


@pytest.mark.parametrize(
    ("instance_name", "category"),
    [
        ("sperm1", "sperm"),
        ("sperm 2", "sperm"),
        ("sperm_3", "sperm"),
        ("sperm-4", "sperm"),
        ("sperm#5", "sperm"),
        ("sperm（6）", "sperm"),
        ("rare sperm", "rare sperm"),
    ],
)
def test_dataset_class_name_strips_only_the_instance_suffix(instance_name: str, category: str) -> None:
    assert _dataset_class_name(instance_name) == category
