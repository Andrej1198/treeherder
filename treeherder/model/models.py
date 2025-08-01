import datetime
import itertools
import logging
import time
import warnings
from hashlib import sha1

import newrelic.agent
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField, TrigramSimilarity
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist
from django.core.validators import MinLengthValidator
from django.db import models, transaction
from django.db.models import Count, Max, Min, Q, Subquery
from django.db.utils import ProgrammingError
from django.forms import model_to_dict
from django.utils import timezone

from treeherder.webapp.api.utils import REPO_GROUPS, to_timestamp

warnings.filterwarnings("ignore", category=DeprecationWarning, module="newrelic")

logger = logging.getLogger(__name__)


class FailuresQuerySet(models.QuerySet):
    def by_bug(self, bugzilla_id):
        return self.filter(bug__bugzilla_id=int(bugzilla_id))

    def by_date(self, startday, endday):
        return self.select_related("push", "job").filter(job__push__time__range=(startday, endday))

    def by_repo(self, name, bugjobmap=True):
        if name in REPO_GROUPS:
            repo = REPO_GROUPS[name]
            return (
                self.filter(job__repository_id__in=repo)
                if bugjobmap
                else self.filter(repository_id__in=repo)
            )
        elif name == "all":
            return self
        else:
            return (
                self.filter(job__repository__name=name)
                if bugjobmap
                else self.filter(repository__name=name)
            )


class NamedModel(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        abstract = True

    def __str__(self):
        return self.name


class Product(NamedModel):
    class Meta:
        db_table = "product"


class BuildPlatform(models.Model):
    id = models.AutoField(primary_key=True)
    os_name = models.CharField(max_length=25)
    platform = models.CharField(max_length=100, db_index=True)
    architecture = models.CharField(max_length=25, blank=True, db_index=True)

    class Meta:
        db_table = "build_platform"
        unique_together = ("os_name", "platform", "architecture")

    def __str__(self):
        return f"{self.os_name} {self.platform} {self.architecture}"


class Option(NamedModel):
    class Meta:
        db_table = "option"


class RepositoryGroup(NamedModel):
    # Fields are pre-defined in fixtures/repository_group.json
    description = models.TextField(blank=True)

    class Meta:
        db_table = "repository_group"


class Repository(models.Model):
    id = models.AutoField(primary_key=True)
    repository_group = models.ForeignKey("RepositoryGroup", on_delete=models.CASCADE)
    name = models.CharField(max_length=50, unique=True, db_index=True)
    dvcs_type = models.CharField(max_length=25, db_index=True)
    url = models.CharField(max_length=255)
    branch = models.CharField(max_length=255, null=True, db_index=True)
    codebase = models.CharField(max_length=50, blank=True, db_index=True)
    description = models.TextField(blank=True)
    active_status = models.CharField(max_length=7, blank=True, default="active", db_index=True)
    life_cycle_order = models.PositiveIntegerField(null=True, default=None)
    performance_alerts_enabled = models.BooleanField(default=False)
    expire_performance_data = models.BooleanField(default=True)
    is_try_repo = models.BooleanField(default=False)
    tc_root_url = models.CharField(max_length=255, null=False, db_index=True)

    class Meta:
        db_table = "repository"
        verbose_name_plural = "repositories"

    @classmethod
    def fetch_all_names(cls) -> list[str]:
        return cls.objects.values_list("name", flat=True)

    def __str__(self):
        return f"{self.name} {self.repository_group}"


class Push(models.Model):
    """
    A push to a repository

    A push should contain one or more commit objects, representing
    the changesets that were part of the push
    """

    repository = models.ForeignKey(Repository, on_delete=models.CASCADE)
    revision = models.CharField(max_length=40, db_index=True)
    author = models.CharField(max_length=150)
    time = models.DateTimeField(db_index=True)

    failures = FailuresQuerySet.as_manager()
    objects = models.Manager()

    class Meta:
        db_table = "push"
        unique_together = ("repository", "revision")

    def __str__(self):
        return f"{self.repository.name} {self.revision}"

    def total_jobs(self, job_type, result):
        return self.jobs.filter(job_type=job_type, result=result).count()

    def get_status(self):
        """
        Gets a summary of what passed/failed for the push
        """
        jobs = (
            Job.objects.filter(push=self)
            .filter(
                Q(failure_classification__isnull=True)
                | Q(failure_classification__name="not classified")
            )
            .exclude(tier=3)
        )

        status_dict = {"completed": 0, "pending": 0, "running": 0}
        for state, result, total in jobs.values_list("state", "result").annotate(
            total=Count("result")
        ):
            if state == "completed":
                status_dict[result] = total
                status_dict[state] += total
            else:
                status_dict[state] = total
        if "superseded" in status_dict:
            # backward compatability for API consumers
            status_dict["coalesced"] = status_dict["superseded"]

        return status_dict


class Commit(models.Model):
    """
    A single commit in a push
    """

    push = models.ForeignKey(Push, on_delete=models.CASCADE, related_name="commits")
    revision = models.CharField(max_length=40, db_index=True)
    author = models.CharField(max_length=150)
    comments = models.TextField()
    search_vector = SearchVectorField(null=True, blank=True)

    class Meta:
        db_table = "commit"
        unique_together = ("push", "revision")
        indexes = [
            GinIndex(fields=["search_vector"], name="search_vector_idx"),
        ]

    def __str__(self):
        return f"{self.push.repository.name} {self.revision}"


class MachinePlatform(models.Model):
    id = models.AutoField(primary_key=True)
    os_name = models.CharField(max_length=25)
    platform = models.CharField(max_length=100, db_index=True)
    architecture = models.CharField(max_length=25, blank=True, db_index=True)

    class Meta:
        db_table = "machine_platform"
        unique_together = ("os_name", "platform", "architecture")

    def __str__(self):
        return f"{self.os_name} {self.platform} {self.architecture}"


class Bugscache(models.Model):
    id = models.BigAutoField(primary_key=True)

    # Optional reference towards a bug in Bugzilla, once is has been reported as occurring many times in a week
    bugzilla_id = models.PositiveIntegerField(null=True, blank=True)

    status = models.CharField(max_length=64, db_index=True)
    resolution = models.CharField(max_length=64, blank=True, db_index=True)
    # Is covered by a FULLTEXT index created via a migrations RunSQL operation.
    summary = models.CharField(max_length=255)
    dupe_of = models.PositiveIntegerField(null=True)
    crash_signature = models.TextField(blank=True)
    keywords = models.TextField(blank=True)
    modified = models.DateTimeField()
    whiteboard = models.CharField(max_length=100, blank=True, default="")
    processed_update = models.BooleanField(default=True)

    class Meta:
        db_table = "bugscache"
        verbose_name_plural = "bugscache"
        indexes = [
            models.Index(fields=["summary"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["bugzilla_id"],
                name="unique_bugzilla_id",
                condition=Q(bugzilla_id__isnull=False),
            )
        ]

    def __str__(self):
        return f"{self.id}"

    def serialize(self):
        exclude_fields = ["modified", "processed_update", "jobmap"]

        attrs = model_to_dict(self, exclude=exclude_fields)
        # Serialize bug ID as the bugzilla number for compatibility reasons
        attrs["internal_id"] = attrs["id"]
        attrs["id"] = attrs.pop("bugzilla_id")

        attrs["occurrences"] = None
        if attrs["id"] is None:
            # Only fetch occurrences for internal issues. It causes one extra query
            attrs["occurrences"] = self.jobmap.filter(
                created__gte=timezone.now()
                - datetime.timedelta(days=settings.INTERNAL_OCCURRENCES_DAYS_WINDOW)
            ).count()
        return attrs

    @classmethod
    def search(cls, search_term):
        max_size = 50
        search_term = search_term.lower()

        # On PostgreSQL we can use the ORM directly, but NOT the full text search
        # as the ranking algorithm expects english words, not paths
        # So we use standard pattern matching AND trigram similarity to compare suite of characters
        # instead of words
        # Django already escapes special characters, so we do not need to handle that here
        recent_qs = (
            Bugscache.objects.filter(summary__icontains=search_term)
            .annotate(similarity=TrigramSimilarity("summary", search_term))
            .order_by("-similarity")[0:max_size]
        )

        try:
            open_recent_match_string = [item.serialize() for item in recent_qs]
            all_data = [
                match
                for match in open_recent_match_string
                if match["summary"].lower().startswith(search_term)
                or "/" + search_term in match["summary"].lower()
                or " " + search_term in match["summary"].lower()
                or "\\" + search_term in match["summary"].lower()
                or "," + search_term in match["summary"].lower()
            ]
            open_recent = [x for x in all_data if x["resolution"] == ""]
            all_others = [x for x in all_data if x["resolution"] != ""]
        except ProgrammingError as e:
            newrelic.agent.notice_error()
            logger.error(
                f"Failed to execute FULLTEXT search on Bugscache, error={e}, SQL={recent_qs.query.__str__()}"
            )
            open_recent = []
            all_others = []

        return {"open_recent": open_recent, "all_others": all_others}


class BugzillaComponent(models.Model):
    product = models.CharField(max_length=60)
    component = models.CharField(max_length=60)

    class Meta:
        db_table = "bugzilla_component"
        verbose_name_plural = "bugzilla_components"
        unique_together = ("product", "component")

    def __str__(self):
        return f"{self.product} :: {self.component}"


class FilesBugzillaMap(models.Model):
    path = models.CharField(max_length=255, unique=True, db_index=True)
    file_name = models.CharField(max_length=255, db_index=True)
    bugzilla_component = models.ForeignKey("BugzillaComponent", on_delete=models.CASCADE)

    class Meta:
        db_table = "file_bugzilla_component"
        verbose_name_plural = "files_bugzilla_components"

    def __str__(self):
        return f"{self.path}"


class BugzillaSecurityGroup(models.Model):
    product = models.CharField(max_length=60, unique=True, db_index=True)
    security_group = models.CharField(max_length=60)

    class Meta:
        db_table = "bugzilla_security_group"
        verbose_name_plural = "bugzilla_security_groups"


class Machine(NamedModel):
    class Meta:
        db_table = "machine"


class JobGroup(models.Model):
    id = models.AutoField(primary_key=True)
    symbol = models.CharField(max_length=25, default="?", db_index=True)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)

    class Meta:
        db_table = "job_group"
        unique_together = ("name", "symbol")

    def __str__(self):
        return f"{self.name} ({self.symbol})"


class OptionCollectionManager(models.Manager):
    cache_key = "option_collection_map"
    """
    Convenience function to determine the option collection map
    """

    def get_option_collection_map(self):
        option_collection_map = cache.get(self.cache_key)

        if option_collection_map:
            return option_collection_map

        option_collection_map = {}
        for hash, option_name in OptionCollection.objects.values_list(
            "option_collection_hash", "option__name"
        ):
            if not option_collection_map.get(hash):
                option_collection_map[hash] = option_name
            else:
                option_collection_map[hash] += " " + option_name

        # Caches for the default of 5 minutes.
        cache.set(self.cache_key, option_collection_map)
        return option_collection_map


class OptionCollection(models.Model):
    id = models.AutoField(primary_key=True)
    option_collection_hash = models.CharField(max_length=40)
    option = models.ForeignKey(Option, on_delete=models.CASCADE, db_index=True)

    objects = OptionCollectionManager()

    @staticmethod
    def calculate_hash(options):
        """returns an option_collection_hash given a list of options"""
        options = sorted(list(options))
        sha_hash = sha1()
        # equivalent to loop over the options and call sha_hash.update()
        sha_hash.update("".join(options).encode("utf-8"))
        return sha_hash.hexdigest()

    class Meta:
        db_table = "option_collection"
        unique_together = ("option_collection_hash", "option")

    def __str__(self):
        return f"{self.option}"


class JobType(models.Model):
    id = models.AutoField(primary_key=True)
    symbol = models.CharField(max_length=25, default="?", db_index=True)
    name = models.CharField(max_length=140)
    description = models.TextField(blank=True)

    class Meta:
        db_table = "job_type"
        unique_together = (("name", "symbol"),)

    def __str__(self):
        return f"{self.name} ({self.symbol})"


class FailureClassification(NamedModel):
    class Meta:
        db_table = "failure_classification"


class ReferenceDataSignatures(models.Model):
    """
    A collection of all the possible combinations of reference data,
    populated on data ingestion. signature is a hash of the data it refers to
    build_system_type is buildbot by default

    TODO: Rename to 'ReferenceDataSignature'.
    """

    name = models.CharField(max_length=255)
    signature = models.CharField(max_length=50, db_index=True)
    build_os_name = models.CharField(max_length=25, db_index=True)
    build_platform = models.CharField(max_length=100, db_index=True)
    build_architecture = models.CharField(max_length=25, db_index=True)
    machine_os_name = models.CharField(max_length=25, db_index=True)
    machine_platform = models.CharField(max_length=100, db_index=True)
    machine_architecture = models.CharField(max_length=25, db_index=True)
    job_group_name = models.CharField(max_length=255, blank=True, db_index=True)
    job_group_symbol = models.CharField(max_length=25, blank=True, db_index=True)
    job_type_name = models.CharField(max_length=140, db_index=True)
    job_type_symbol = models.CharField(max_length=25, blank=True, db_index=True)
    option_collection_hash = models.CharField(max_length=64, blank=True, db_index=True)
    build_system_type = models.CharField(max_length=25, blank=True, db_index=True)
    repository = models.CharField(max_length=50, db_index=True)
    first_submission_timestamp = models.IntegerField(db_index=True)

    class Meta:
        db_table = "reference_data_signatures"
        # Remove if/when the model is renamed to 'ReferenceDataSignature'.
        verbose_name_plural = "reference data signatures"
        unique_together = ("name", "signature", "build_system_type", "repository")


class JobManager(models.Manager):
    """
    Convenience functions for operations on groups of jobs
    """

    def cycle_data(self, cycle_interval, chunk_size, sleep_time):
        """
        Delete data older than cycle_interval, splitting the target data into
        chunks of chunk_size size. Returns the number of result sets deleted
        """
        jobs_max_timestamp = datetime.datetime.now() - cycle_interval

        jobs_cycled = 0
        while True:
            min_id = Job.objects.aggregate(Min("id"))["id__min"]
            if min_id is None:
                return jobs_cycled
            max_id = min_id + chunk_size
            max_chunk = Job.objects.filter(id__lt=max_id).aggregate(
                submit_time=Max("submit_time"), count=Count("id")
            )
            if max_chunk["count"] == 0 or max_chunk["submit_time"] > jobs_max_timestamp:
                # this next chunk is too young, we are done
                return jobs_cycled

            logger.warning(
                "Pruning jobs: chunk of {} older than {}".format(
                    max_chunk["count"], jobs_max_timestamp.strftime("%b %d %Y")
                )
            )

            # Remove ORM entries for these jobs that don't currently have a
            # foreign key relation
            logger.warning("deleting FailureLines")
            delete_guid = list(
                Job.objects.filter(id__lt=max_id).only("guid").values_list("guid", flat=True)
            )
            FailureLine.objects.filter(job_guid__in=delete_guid).only("id").delete()

            # cycle jobs *after* related data has been deleted, to be sure
            # we don't have any orphan data
            logger.warning("delete jobs")
            self.filter(id__lt=max_id).only("id").delete()

            jobs_cycled += max_chunk["count"]

            if sleep_time:
                # Allow some time for other queries to get through
                time.sleep(sleep_time)


class Job(models.Model):
    """
    This class represents a build or test job in Treeherder
    """

    failures = FailuresQuerySet.as_manager()
    objects = JobManager()

    id = models.BigAutoField(primary_key=True)

    PENDING = 0
    CROSSREFERENCED = 1
    AUTOCLASSIFIED = 2
    SKIPPED = 3
    FAILED = 255

    AUTOCLASSIFY_STATUSES = (
        (PENDING, "pending"),
        (CROSSREFERENCED, "crossreferenced"),
        (AUTOCLASSIFIED, "autoclassified"),
        (SKIPPED, "skipped"),
        (FAILED, "failed"),
    )

    repository = models.ForeignKey(Repository, on_delete=models.CASCADE)
    guid = models.CharField(max_length=50, unique=True)
    project_specific_id = models.PositiveIntegerField(null=True)  # unused, see bug 1328985
    # TODO: Remove autoclassify_status next time jobs table is modified (bug 1594822)
    autoclassify_status = models.IntegerField(choices=AUTOCLASSIFY_STATUSES, default=PENDING)

    # TODO: Remove coalesced_to_guid next time the jobs table is modified (bug 1402992)
    coalesced_to_guid = models.CharField(max_length=50, null=True, default=None)
    signature = models.ForeignKey(ReferenceDataSignatures, on_delete=models.CASCADE)
    build_platform = models.ForeignKey(BuildPlatform, on_delete=models.CASCADE, related_name="jobs")
    machine_platform = models.ForeignKey(MachinePlatform, on_delete=models.CASCADE)
    machine = models.ForeignKey(Machine, on_delete=models.CASCADE)
    option_collection_hash = models.CharField(max_length=64)
    job_type = models.ForeignKey(JobType, on_delete=models.CASCADE, related_name="jobs")
    job_group = models.ForeignKey(JobGroup, on_delete=models.CASCADE, related_name="jobs")
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    failure_classification = models.ForeignKey(
        FailureClassification, on_delete=models.CASCADE, related_name="jobs"
    )
    who = models.CharField(max_length=50)
    reason = models.CharField(max_length=125)
    result = models.CharField(max_length=25)
    state = models.CharField(max_length=25)

    submit_time = models.DateTimeField()
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    last_modified = models.DateTimeField(auto_now=True, db_index=True)
    # TODO: Remove next time we add/drop another field.
    running_eta = models.PositiveIntegerField(null=True, default=None)
    tier = models.PositiveIntegerField()

    push = models.ForeignKey(Push, on_delete=models.CASCADE, related_name="jobs")

    class Meta:
        db_table = "job"
        indexes = [
            # these speed up the various permutations of the "similar jobs"
            # queries
            models.Index(fields=["repository", "job_type", "start_time"]),
            models.Index(fields=["repository", "build_platform", "job_type", "start_time"]),
            models.Index(fields=["repository", "option_collection_hash", "job_type", "start_time"]),
            models.Index(
                fields=[
                    "repository",
                    "build_platform",
                    "option_collection_hash",
                    "job_type",
                    "start_time",
                ]
            ),
            # this is intended to speed up queries for specific platform /
            # option collections on a push
            models.Index(fields=["machine_platform", "option_collection_hash", "push"]),
            # speed up cycle data
            models.Index(fields=["repository", "submit_time"]),
        ]

    @property
    def tier_is_sheriffable(self) -> bool:
        """
        Tier 3 jobs are not considered stable enough to be sheriffed.
        """
        return self.tier < 3

    def __str__(self):
        return f"{self.id} {self.repository} {self.guid}"

    def get_platform_option(self, option_collection_map=None):
        if not hasattr(self, "platform_option"):
            self.platform_option = ""
            option_hash = self.option_collection_hash
            if option_hash:
                if not option_collection_map:
                    option_collection_map = OptionCollection.objects.get_option_collection_map()
                self.platform_option = option_collection_map.get(option_hash)

        return self.platform_option

    def save(self, *args, update_fields=None, **kwargs):
        self.last_modified = datetime.datetime.now()

        if update_fields is not None:
            update_fields = {"last_modified"}.union(update_fields)

        super().save(*args, update_fields=update_fields, **kwargs)

    def get_manual_classification_line(self):
        """
        If this Job has a single TextLogError line, return that TextLogError.

        Some Jobs only have one related TextLogError.  This
        method checks if this Job is one of those (returning None if not) by:
        * checking the number of related TextLogErrors
        * counting the number of search results for the single TextLogError
        * checking there is a related FailureLine
        * checking the related FailureLine is in a given state

        If all these checks pass the TextLogError is returned, any failure returns None.
        """
        try:
            text_log_error = TextLogError.objects.get(job=self)
        except (TextLogError.DoesNotExist, TextLogError.MultipleObjectsReturned):
            return None

        # Can this TextLogError be converted into a single "useful search"?
        # FIXME: what is the significance of only one search result here?
        from treeherder.model.error_summary import get_useful_search_results

        search_results = get_useful_search_results(self)
        if len(search_results) != 1:
            return None

        # Check that we have a related FailureLine
        failure_line = text_log_error.get_failure_line()
        if failure_line is None:
            return None

        # Check our FailureLine is in a state we expect for
        # auto-classification.
        if not (
            failure_line.action == "test_result"
            and failure_line.test
            and failure_line.status
            and failure_line.expected
        ):
            return None

        return text_log_error

    def fetch_associated_decision_job(self):
        decision_type = JobType.objects.filter(name="Gecko Decision Task", symbol="D")
        return Job.objects.get(
            repository_id=self.repository_id,
            job_type_id=Subquery(decision_type.values("id")[:1]),
            push_id=self.push_id,
        )

    @staticmethod
    def get_duration(submit_time, start_time, end_time):
        endtime = end_time if to_timestamp(end_time) else datetime.datetime.now()
        starttime = start_time if to_timestamp(start_time) else submit_time
        seconds = max((endtime - starttime).total_seconds(), 60)
        return max(round(seconds / 60), 1)


class TaskclusterMetadata(models.Model):
    """
    Taskcluster-specific metadata associated with a taskcluster job
    """

    job = models.OneToOneField(
        Job, on_delete=models.CASCADE, primary_key=True, related_name="taskcluster_metadata"
    )

    task_id = models.CharField(max_length=22, validators=[MinLengthValidator(22)], db_index=True)
    retry_id = models.PositiveIntegerField()

    class Meta:
        db_table = "taskcluster_metadata"


class JobLog(models.Model):
    """
    Represents a log associated with a job

    There can be more than one of these associated with each job
    """

    PENDING = 0
    PARSED = 1
    FAILED = 2
    SKIPPED_SIZE = 3

    STATUSES = (
        (PENDING, "pending"),
        (PARSED, "parsed"),
        (FAILED, "failed"),
        (SKIPPED_SIZE, "skipped-size"),
    )

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="job_log")
    name = models.CharField(max_length=50)
    url = models.URLField(max_length=255)
    status = models.IntegerField(choices=STATUSES, default=PENDING)

    class Meta:
        db_table = "job_log"
        unique_together = ("job", "name", "url")

    def __str__(self):
        return f"{self.id} {self.job.guid} {self.name} {self.status}"

    def update_status(self, status):
        self.status = status
        self.save(update_fields=["status"])


class BugJobMap(models.Model):
    """
    Maps job_ids to related bug. It supports referencing an internal issue (bug without a Bugzilla ID).

    Mappings can be made manually through a UI or from doing lookups in the
    BugsCache
    """

    id = models.BigAutoField(primary_key=True)

    job = models.ForeignKey(Job, on_delete=models.CASCADE)
    bug = models.ForeignKey("Bugscache", on_delete=models.CASCADE, related_name="jobmap")
    created = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True)  # null if autoclassified
    bug_open = models.BooleanField(default=False)

    failures = FailuresQuerySet.as_manager()
    objects = models.Manager()

    class Meta:
        db_table = "bug_job_map"
        constraints = [
            models.UniqueConstraint(
                fields=["job", "bug"],
                name="unique_job_bug_mapping",
            )
        ]

    @property
    def who(self):
        if self.user:
            return self.user.email
        else:
            return "autoclassifier"

    @classmethod
    def create(cls, *, job_id, internal_bug_id=None, bugzilla_id=None, user=None, bug_open=False):
        if (bool(internal_bug_id) ^ bool(bugzilla_id)) is False:
            raise ValueError("Only one of internal bug ID or Bugzilla ID must be set")
        if internal_bug_id:
            bug_reference = {"bug_id": internal_bug_id}
        else:
            # Try mapping Bugzilla ID to internal reference of a bug
            try:
                bug_reference = {
                    "bug_id": Bugscache.objects.only("id").get(bugzilla_id=bugzilla_id).id
                }
            except Bugscache.DoesNotExist:
                raise ValueError(f"No bug found with Bugzilla ID {bugzilla_id}")

        bug_map = BugJobMap.objects.create(
            job_id=job_id, user=user, bug_open=bug_open, **bug_reference
        )

        if not user:
            return bug_map

        # We have a user so this wasn't triggered by auto-classification.
        # However we need to update the ClassifiedFailure with the bug number
        # we just used to create the BugJobMap.

        text_log_error = bug_map.job.get_manual_classification_line()
        if text_log_error is None:
            return bug_map

        classification = (
            text_log_error.metadata.best_classification if text_log_error.metadata else None
        )

        if (
            bugzilla_id
            # no classification to update
            and classification is not None
            # classification already has a bug number
            and not classification.bug_number
        ):
            classification.set_bug(bugzilla_id)

        return bug_map

    def __str__(self):
        return f"{self.id} {self.job.guid} {self.bug_id} {self.user}"


class JobNote(models.Model):
    """
    Associates a Failure type with a Job and optionally a text comment from a User.

    Generally these are generated manually in the UI.
    """

    id = models.BigAutoField(primary_key=True)

    job = models.ForeignKey(Job, on_delete=models.CASCADE)
    failure_classification = models.ForeignKey(FailureClassification, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True)  # null if autoclassified
    text = models.TextField()
    created = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "job_note"

    @property
    def who(self):
        if self.user:
            return self.user.email
        return "autoclassifier"

    def _update_failure_type(self):
        """
        Updates the failure type of this Note's Job.

        Set the linked Job's failure type to that of the most recent JobNote or
        set to Not Classified if there are no JobNotes.

        This is called when JobNotes are created (via .save()) and deleted (via
        .delete()) and is used to resolved the FailureClassification which has
        been denormalised onto Job.
        """
        # update the job classification
        note = JobNote.objects.filter(job=self.job).order_by("-created").first()
        if note:
            self.job.failure_classification_id = note.failure_classification.id
        else:
            self.job.failure_classification_id = FailureClassification.objects.get(
                name="not classified"
            ).id
        self.job.save()

    def _ensure_classification(self):
        """
        Ensures a single TextLogError's related bugs have Classifications.

        If the linked Job has a single meaningful TextLogError:
         - find the bugs currently related to it via a Classification
         - find the bugs mapped to the job related to this note
         - find the bugs that are mapped but not classified
         - link this subset of bugs to Classifications
         - if there's only one new bug and no existing ones, verify it
        """
        # if this note was automatically filed, don't update the auto-classification information
        if not self.user:
            return

        # if the failure type isn't intermittent, ignore
        if self.failure_classification.name not in ["intermittent", "intermittent needs bugid"]:
            return

        # if the linked Job has more than one TextLogError, ignore
        text_log_error = self.job.get_manual_classification_line()
        if not text_log_error:
            return

        # evaluate the QuerySet here so it can be used when creating new_bugs below
        existing_bugs = list(
            ClassifiedFailure.objects.filter(
                error_matches__text_log_error=text_log_error
            ).values_list("bug_number", flat=True)
        )

        new_bugs = self.job.bugjobmap_set.exclude(bug_id__in=existing_bugs).values_list(
            "bug_id", flat=True
        )

        if not new_bugs:
            return

        # Create Match instances for each new bug
        for bug_number in new_bugs:
            classification, _ = ClassifiedFailure.objects.get_or_create(bug_number=bug_number)
            text_log_error.create_match("ManualDetector", classification)

        # if there's only one new bug and no existing ones, verify it
        if len(new_bugs) == 1 and not existing_bugs:
            text_log_error.verify_classification(classification)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self._update_failure_type()
        self._ensure_classification()

    def delete(self, *args, **kwargs):
        super().delete(*args, **kwargs)
        self._update_failure_type()
        self._ensure_classification()

    def __str__(self):
        return f"{self.id} {self.job.guid} {self.failure_classification} {self.who}"


class FailureLine(models.Model):
    # We make use of prefix indicies for several columns in this table which
    # can't be expressed in django syntax so are created with raw sql in migrations.
    STATUS_LIST = ("PASS", "FAIL", "OK", "ERROR", "TIMEOUT", "CRASH", "ASSERT", "SKIP", "NOTRUN")
    # Truncated is a special action that we use to indicate that the list of failure lines
    # was truncated according to settings.FAILURE_LINES_CUTOFF.
    ACTION_LIST = ("test_result", "log", "crash", "truncated", "group_result")
    LEVEL_LIST = ("critical", "error", "warning", "info", "debug")

    # Python 3's zip produces an iterable rather than a list, which Django's `choices` can't handle.
    ACTION_CHOICES = list(zip(ACTION_LIST, ACTION_LIST))
    STATUS_CHOICES = list(zip(STATUS_LIST, STATUS_LIST))
    LEVEL_CHOICES = list(zip(LEVEL_LIST, LEVEL_LIST))

    id = models.BigAutoField(primary_key=True)
    job_guid = models.CharField(max_length=50)
    repository = models.ForeignKey(Repository, on_delete=models.CASCADE)
    job_log = models.ForeignKey(
        JobLog, on_delete=models.CASCADE, null=True, related_name="failure_line"
    )
    action = models.CharField(max_length=15, choices=ACTION_CHOICES)
    line = models.PositiveIntegerField()
    test = models.TextField(blank=True, null=True)
    subtest = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=7, choices=STATUS_CHOICES)
    expected = models.CharField(max_length=7, choices=STATUS_CHOICES, blank=True, null=True)
    message = models.TextField(blank=True, null=True)
    signature = models.TextField(blank=True, null=True)
    level = models.CharField(max_length=8, choices=STATUS_CHOICES, blank=True, null=True)
    stack = models.TextField(blank=True, null=True)
    stackwalk_stdout = models.TextField(blank=True, null=True)
    stackwalk_stderr = models.TextField(blank=True, null=True)

    # Note that the case of best_classification = None and best_is_verified = True
    # has the special semantic that the line is ignored and should not be considered
    # for future autoclassifications.
    best_classification = models.ForeignKey(
        "ClassifiedFailure",
        related_name="best_for_lines",
        null=True,
        db_index=True,
        on_delete=models.SET_NULL,
    )

    best_is_verified = models.BooleanField(default=False)

    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "failure_line"
        indexes = [models.Index(fields=["job_guid", "repository"])]
        unique_together = ("job_log", "line")

    def __str__(self):
        return f"{self.id} {Job.objects.get(guid=self.job_guid).id}"

    @property
    def error(self):
        # Return the related text-log-error or None if there is no related field.
        try:
            return self.text_log_error_metadata.text_log_error
        except TextLogErrorMetadata.DoesNotExist:
            return None

    def _serialized_components(self):
        if self.action == "test_result":
            return [f"TEST-UNEXPECTED-{self.status.upper()}", self.test]
        if self.action == "log":
            return [self.level.upper(), self.message.split("\n")[0]]

    def unstructured_bugs(self):
        """
        Get bugs that match this line in the Bug Suggestions artifact for this job.
        """
        components = self._serialized_components()
        if not components:
            return []

        from treeherder.model.error_summary import get_useful_search_results

        job = Job.objects.get(guid=self.job_guid)
        rv = []
        ids_seen = set()
        for item in get_useful_search_results(job):
            if all(component in item["search"] for component in components):
                for suggestion in itertools.chain(
                    item["bugs"]["open_recent"], item["bugs"]["all_others"]
                ):
                    if suggestion["id"] not in ids_seen:
                        ids_seen.add(suggestion["id"])
                        rv.append(suggestion)

        return rv

    def to_dict(self):
        try:
            metadata = self.text_log_error_metadata
        except ObjectDoesNotExist:
            metadata = None

        return {
            "id": self.id,
            "job_guid": self.job_guid,
            "repository": self.repository_id,
            "job_log": self.job_log_id,
            "action": self.action,
            "line": self.line,
            "test": self.test,
            "subtest": self.subtest,
            "status": self.status,
            "expected": self.expected,
            "message": self.message,
            "signature": self.signature,
            "level": self.level,
            "stack": self.stack,
            "stackwalk_stdout": self.stackwalk_stdout,
            "stackwalk_stderr": self.stackwalk_stderr,
            "best_classification": metadata.best_classification_id if metadata else None,
            "best_is_verified": metadata.best_is_verified if metadata else False,
            "created": self.created,
            "modified": self.modified,
        }

    def to_mozlog_format(self):
        """Convert a FailureLine into a mozlog formatted dictionary."""
        data = {
            "action": self.action,
            "line_number": self.line,
            "test": self.test,
            "subtest": self.subtest,
            "status": self.status,
            "expected": self.expected,
            "message": self.message,
            "signature": self.signature,
            "level": self.level,
            "stack": self.stack,
            "stackwalk_stdout": self.stackwalk_stdout,
            "stackwalk_stderr": self.stackwalk_stderr,
        }

        # Remove empty values
        data = {k: v for k, v in data.items() if v is not None}

        return data


class Group(models.Model):
    """
    The test harness group.

    This is most often a manifest file.  But in some instances where a suite
    doesn't have manifests, or a test suite isn't logging its data properly,
    this can simply be "default"

    Note: This is not to be confused with JobGroup which is Treeherder specific.
    """

    id = models.BigAutoField(primary_key=True)
    name = models.CharField(max_length=255, unique=True)
    job_logs = models.ManyToManyField("JobLog", through="GroupStatus", related_name="groups")

    def __str__(self):
        return self.name

    class Meta:
        db_table = "group"


class GroupStatus(models.Model):
    OK = 1
    ERROR = 2
    SKIP = 3
    UNSUPPORTED = 10
    STATUS_MAP = {"OK": OK, "ERROR": ERROR, "SKIP": SKIP}
    STATUS_LOOKUP = {OK: "OK", ERROR: "ERROR", SKIP: "SKIP"}

    status = models.SmallIntegerField()
    job_log = models.ForeignKey(JobLog, on_delete=models.CASCADE, related_name="group_result")
    group = models.ForeignKey(Group, on_delete=models.CASCADE, related_name="group_result")
    duration = models.SmallIntegerField()

    @staticmethod
    def get_status(status_str):
        return (
            GroupStatus.STATUS_MAP[status_str]
            if status_str in GroupStatus.STATUS_MAP
            else GroupStatus.UNSUPPORTED
        )

    class Meta:
        db_table = "group_status"


class ClassifiedFailure(models.Model):
    """
    Classifies zero or more TextLogErrors as a failure.

    Optionally linked to a bug.
    """

    id = models.BigAutoField(primary_key=True)
    text_log_errors = models.ManyToManyField(
        "TextLogError", through="TextLogErrorMatch", related_name="classified_failures"
    )
    # Note that we use a bug number of 0 as a sentinel value to indicate lines that
    # are not actually symptomatic of a real bug
    bug_number = models.PositiveIntegerField(blank=True, null=True, unique=True)
    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.id} {self.bug_number}"

    def bug(self):
        # Putting this here forces one query per object; there should be a way
        # to make things more efficient
        return Bugscache.objects.filter(bugzilla_id=self.bug_number).first()

    def set_bug(self, bug_number):
        """
        Set the bug number of this Classified Failure

        If an existing ClassifiedFailure exists with the same bug number
        replace this instance with the existing one.
        """
        if bug_number == self.bug_number:
            return self

        other = ClassifiedFailure.objects.filter(bug_number=bug_number).first()
        if not other:
            self.bug_number = bug_number
            self.save(update_fields=["bug_number"])
            return self

        self.replace_with(other)

        return other

    @transaction.atomic
    def replace_with(self, other):
        """
        Replace this instance with the given other.

        Deletes stale Match objects and updates related TextLogErrorMetadatas'
        best_classifications to point to the given other.
        """
        match_ids_to_delete = list(self.update_matches(other))
        TextLogErrorMatch.objects.filter(id__in=match_ids_to_delete).delete()

        # Update best classifications
        self.best_for_errors.update(best_classification=other)

        self.delete()

    def update_matches(self, other):
        """
        Update this instance's Matches to point to the given other's Matches.

        Find Matches with the same TextLogError as our Matches, updating their
        score if less than ours and mark our matches for deletion.

        If there are no other matches, update ours to point to the other
        ClassifiedFailure.
        """
        for match in self.error_matches.all():
            other_matches = TextLogErrorMatch.objects.filter(
                classified_failure=other,
                text_log_error=match.text_log_error,
            )

            if not other_matches:
                match.classified_failure = other
                match.save(update_fields=["classified_failure"])
                continue

            # if any of our matches have higher scores than other's matches,
            # overwrite with our score.
            other_matches.filter(score__lt=match.score).update(score=match.score)

            yield match.id  # for deletion

    class Meta:
        db_table = "classified_failure"


class TextLogError(models.Model):
    """
    A detected error line in the textual (unstructured) log
    """

    id = models.BigAutoField(primary_key=True)
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="text_log_error", null=True)
    line = models.TextField()
    line_number = models.PositiveIntegerField()
    new_failure = models.BooleanField(default=False)

    class Meta:
        db_table = "text_log_error"
        unique_together = ("job", "line_number")

    def __str__(self):
        return f"{self.id} {self.job.id}"

    @property
    def metadata(self):
        try:
            return self._metadata
        except TextLogErrorMetadata.DoesNotExist:
            return None

    def bug_suggestions(self):
        from treeherder.model import error_summary

        return error_summary.bug_suggestions_line(
            self, self.job.repository, revision=self.job.push.revision
        )

    def create_match(self, matcher_name, classification):
        """
        Create a TextLogErrorMatch instance

        Typically used for manual "matches" or tests.
        """
        if classification is None:
            classification = ClassifiedFailure.objects.create()

        TextLogErrorMatch.objects.create(
            text_log_error=self,
            classified_failure=classification,
            matcher_name=matcher_name,
            score=1,
        )

    def verify_classification(self, classification):
        """
        Mark the given ClassifiedFailure as verified.

        Handles the classification not currently being related to this
        TextLogError and no Metadata existing.
        """
        if classification not in self.classified_failures.all():
            self.create_match("ManualDetector", classification)

        # create a TextLogErrorMetadata instance for this TextLogError if it
        # doesn't exist.  We can't use update_or_create here since OneToOne
        # relations don't use an object manager so a missing relation is simply
        # None as opposed to RelatedManager.
        if self.metadata is None:
            TextLogErrorMetadata.objects.create(
                text_log_error=self, best_classification=classification, best_is_verified=True
            )
        else:
            self.metadata.best_classification = classification
            self.metadata.best_is_verified = True
            self.metadata.save(update_fields=["best_classification", "best_is_verified"])

        # Send event to NewRelic when a verifing an autoclassified failure.
        match = self.matches.filter(classified_failure=classification).first()
        if not match:
            return

        newrelic.agent.record_custom_event(
            "user_verified_classification",
            {
                "matcher": match.matcher_name,
                "job_id": self.id,
            },
        )

    def get_failure_line(self):
        """Get a related FailureLine instance if one exists."""
        try:
            return self.metadata.failure_line
        except AttributeError:
            return None


class TextLogErrorMetadata(models.Model):
    """
    Link matching TextLogError and FailureLine instances.

    Tracks best classification and verificiation of a classification.

    TODO: Merge into TextLogError.
    """

    text_log_error = models.OneToOneField(
        TextLogError, primary_key=True, related_name="_metadata", on_delete=models.CASCADE
    )

    failure_line = models.OneToOneField(
        FailureLine, on_delete=models.CASCADE, related_name="text_log_error_metadata", null=True
    )

    # Note that the case of best_classification = None and best_is_verified = True
    # has the special semantic that the line is ignored and should not be considered
    # for future autoclassifications.
    best_classification = models.ForeignKey(
        ClassifiedFailure, related_name="best_for_errors", null=True, on_delete=models.SET_NULL
    )
    best_is_verified = models.BooleanField(default=False)

    class Meta:
        db_table = "text_log_error_metadata"

    def __str__(self):
        args = (self.text_log_error_id, self.failure_line_id)
        return "TextLogError={} FailureLine={}".format(*args)


class TextLogErrorMatch(models.Model):
    """Association table between TextLogError and ClassifiedFailure, containing
    additional data about the association including the matcher that was used
    to create it and a score in the range 0-1 for the goodness of match."""

    id = models.BigAutoField(primary_key=True)
    text_log_error = models.ForeignKey(
        TextLogError, related_name="matches", on_delete=models.CASCADE
    )
    classified_failure = models.ForeignKey(
        ClassifiedFailure, related_name="error_matches", on_delete=models.CASCADE
    )

    matcher_name = models.CharField(max_length=255)
    score = models.DecimalField(max_digits=3, decimal_places=2, blank=True, null=True)

    class Meta:
        db_table = "text_log_error_match"
        verbose_name_plural = "text log error matches"
        unique_together = ("text_log_error", "classified_failure", "matcher_name")

    def __str__(self):
        return f"{self.text_log_error.id} {self.classified_failure.id}"


class InvestigatedTests(models.Model):
    """Tests that have been marked as investigated in the Push health UI.  These act like
    completed To Do List items."""

    id = models.BigAutoField(primary_key=True)
    job_type = models.ForeignKey(JobType, related_name="investigated", on_delete=models.CASCADE)
    test = models.CharField(max_length=350)
    push = models.ForeignKey(Push, on_delete=models.CASCADE)

    class Meta:
        unique_together = ["job_type", "test", "push"]
        db_table = "investigated_tests"


class MozciClassification(models.Model):
    """
    Automated classification of a Push provided by mozci
    """

    BAD = "BAD"
    GOOD = "GOOD"
    UNKNOWN = "UNKNOWN"

    CLASSIFICATION_RESULT = (
        (BAD, "bad"),
        (GOOD, "good"),
        (UNKNOWN, "unknown"),
    )

    id = models.BigAutoField(primary_key=True)
    push = models.ForeignKey(Push, on_delete=models.CASCADE)
    result = models.CharField(max_length=7, choices=CLASSIFICATION_RESULT)
    created = models.DateTimeField(default=timezone.now)
    task_id = models.CharField(max_length=22, validators=[MinLengthValidator(22)])

    class Meta:
        db_table = "mozci_classification"
