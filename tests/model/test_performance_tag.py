import pytest
from django.db.utils import IntegrityError

from treeherder.perf.models import PerformanceTag


def test_performance_tags_cannot_have_duplicate_names(transactional_db):
    PerformanceTag.objects.create(name="harness")

    with pytest.raises(IntegrityError):
        PerformanceTag.objects.create(name="harness")
