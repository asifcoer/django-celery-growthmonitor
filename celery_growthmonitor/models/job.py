import os
import random as rnd
import re
from distutils.version import StrictVersion
from enum import unique

from autoslug import AutoSlugField
from django import get_version as django_version
from django.core.validators import RegexValidator
from django.db import models
from django.utils.translation import ugettext_lazy as _

from .. import settings

TEMPORARY_JOB_FOLDER = 'tmp'


def _user_path(attribute_or_prefix, filename=''):
    if attribute_or_prefix:
        if callable(attribute_or_prefix):
            # It's a callable attribute
            co_code = attribute_or_prefix.__code__.co_code
            if co_code != job_data.__code__.co_code and co_code != job_results.__code__.co_code:
                # Prevent infinite recursion if using job_data(...) or job_results(...)
                return attribute_or_prefix(filename)
        else:
            # It's a prefix
            return os.path.join(attribute_or_prefix, filename)
    return None


def root_job(instance):
    """
    Return the root path in the filesystem for the job `instance` folder.

    Parameters
    ----------
    instance : AJob
        The model instance associated with the file

    Returns
    --------
    str
        Path to the root folder for that job
    """
    if instance.job_root:
        if callable(instance.job_root):
            return instance.job_root()
        else:
            head = str(instance.job_root)
    else:
        head = os.path.join(settings.APP_ROOT, instance.__class__.__name__.lower())
    if not instance.id or (
            instance.id and getattr(instance, '_tmp_id', None) and not getattr(instance, '_tmp_files', None)):
        # Assuming we are using JobWithRequiredUserFilesManager
        assert hasattr(instance, '_tmp_id'), "Please use the {} manager".format(JobWithRequiredUserFilesManager)
        tail = os.path.join(TEMPORARY_JOB_FOLDER, str(getattr(instance, '_tmp_id')))
    else:
        tail = str(instance.id)
    return os.path.join(head, tail)


def job_root(instance, filename=''):
    """
    Return the path of `filename` stored at the root folder of his job `instance`.

    Parameters
    ----------
    instance : AJob
        The model instance associated
    filename : str
        Original filename

    Returns
    --------
    str
        Path to filename which is unique for a job
    """
    return os.path.join(root_job(instance), filename)


def job_data(instance, filename=''):
    """
    Return the path of `filename` stored in a subfolder of the root folder of his job `instance`.

    Parameters
    ----------
    instance : AJob or ADataFile
        The model instance associated
    filename : str
        Original filename

    Returns
    --------
    str
        Path to filename which is unique for a job
    """
    head = root_job(instance.job) if isinstance(instance, ADataFile) else root_job(instance)
    tail = _user_path(instance.upload_to_data, filename) or os.path.join('data', filename)
    return os.path.join(head, tail)


def job_results(instance, filename=''):
    """
    Return the path of `filename` stored in a subfolder of the root folder of his job `instance`.

    Parameters
    ----------
    instance : AJob
        The model instance associated
    filename : str
        Original filename

    Returns
    --------
    str
        Path to filename which is unique for a job
    """
    tail = _user_path(instance.upload_to_results, filename) or os.path.join('results', filename)
    return os.path.join(root_job(instance), tail)


def move_to_data(sender, instance, created, **kwargs):
    # https://stackoverflow.com/a/16574947/
    if not hasattr(instance, 'required_user_files'):
        raise AttributeError(
            "{} is not set properly, please set {} as manager".format(sender, JobWithRequiredUserFilesManager))
    if created:
        # TODO: assert required_user_files is not empty? --> user warning?
        setattr(instance, '_tmp_files', list(getattr(instance, 'required_user_files')))
        for field in instance.required_user_files:
            file = getattr(instance, field) if isinstance(field, str) else getattr(instance, field.attname)
            if not file:
                raise FileNotFoundError("{} is indicated as required, but no file could be found".format(field))
            # Create new filename, using primary key and file extension
            old_filename = file.name
            new_filename = file.field.upload_to(instance, os.path.basename(old_filename))
            # Create new file and remove old one
            file.storage.save(new_filename, file)
            file.name = new_filename
            file.close()
            file.storage.delete(old_filename)
            getattr(instance, '_tmp_files').remove(field)
        import shutil
        shutil.rmtree(root_job(instance))
        setattr(instance, '_tmp_id', 0)


class JobWithRequiredUserFilesManager(models.Manager):
    def contribute_to_class(self, model, name):
        super(JobWithRequiredUserFilesManager, self).contribute_to_class(model, name)
        setattr(model, 'upload_to_data', getattr(model, 'upload_to_data', None))
        setattr(model, 'required_user_files', getattr(model, 'required_user_files', []))
        setattr(model, '_tmp_id', rnd.randrange(10 ** 6, 10 ** 7))
        models.signals.post_save.connect(move_to_data, model)


class AJob(models.Model):
    """
    See Also
    --------
    http://stackoverflow.com/questions/16655097/django-abstract-models-versus-regular-inheritance#16838663
    """

    from echoices.enums import EChoice
    from echoices.fields import make_echoicefield

    class Meta:
        abstract = True

    @unique
    class EStates(EChoice):
        # Creation codes
        CREATED = (0, 'Created')
        # Submission codes
        SUBMITTED = (100, 'Submitted')
        # Computation codes
        RUNNING = (200, 'Running')
        # Completion codes
        COMPLETED = (300, 'Completed')

    @unique
    class EStatuses(EChoice):
        ACTIVE = (0, 'Active')
        SUCCESS = (10, 'Succeeded')
        FAILURE = (20, 'Failed')

    IDENTIFIER_MIN_LENGTH = 0
    IDENTIFIER_MAX_LENGTH = 32
    IDENTIFIER_ALLOWED_CHARS = "[a-zA-Z0-9]"
    IDENTIFIER_REGEX = re.compile("{}{{{},}}".format(IDENTIFIER_ALLOWED_CHARS, IDENTIFIER_MIN_LENGTH))
    SLUG_MAX_LENGTH = 32
    SLUG_RND_LENGTH = 6

    job_root = None
    upload_to_results = None

    def slug_default(self):
        if self.identifier:
            slug = self.identifier[:min(len(self.identifier), self.SLUG_RND_LENGTH)]
        else:
            slug = self.__class__.__name__[0]
        slug += self.timestamp.strftime("%y%m%d%H%M")  # YYMMDDHHmm
        if len(slug) > self.SLUG_MAX_LENGTH:
            slug = slug[:self.SLUG_MAX_LENGTH - self.SLUG_RND_LENGTH] + \
                   str(rnd.randrange(10 ** (self.SLUG_RND_LENGTH - 1), 10 ** self.SLUG_RND_LENGTH))
        # TODO: assert uniqueness, otherwise regen
        return slug

    timestamp = models.DateTimeField(verbose_name=_("Job creation timestamp"), auto_now_add=True)
    # TODO: validate identifier over allowance for slug or [a-zA-Z0-9_]
    identifier = models.CharField(
        max_length=IDENTIFIER_MAX_LENGTH,
        blank=True,
        db_index=True,
        help_text=_("Human readable identifier, as provided by the submitter"),
        validators=[RegexValidator(regex=IDENTIFIER_REGEX)])
    state = make_echoicefield(EStates, default=EStates.CREATED, editable=False)
    status = make_echoicefield(EStatuses, default=EStatuses.ACTIVE, editable=False)
    duration = models.DurationField(null=True, editable=False)
    slug = AutoSlugField(
        max_length=SLUG_MAX_LENGTH,
        unique=True,
        editable=True,
        populate_from=slug_default,
        db_index=True,
        help_text=_("Human readable url, must be unique, a default one will be generated if none is given"))
    closure = models.DateTimeField(
        blank=True,
        null=True,
        db_index=True,
        help_text=_("Timestamp of removal, will be set automatically on creation if not given")
    )  # Default is set on save()

    def __str__(self):
        return str('{} {} ({} and {})'.format(self.__class__.__name__, self.id, self.state.label, self.status.label))

    def save(self, *args, results_exist_ok=False, **kwargs):
        created = not self.id
        super(AJob, self).save(*args, **kwargs)  # Call the "real" save() method.
        if created:
            # Set timeout
            self.closure = self.timestamp + settings.TTL
            super(AJob, self).save(*args, **kwargs)  # Write closure to DB
            # Ensure the destination folder exists (may create some issues else, depending on application usage)
            os.makedirs(os.path.join(settings.django_settings.MEDIA_ROOT, job_results(self)), exist_ok=results_exist_ok)


class ADataFile(models.Model):
    class Meta:
        abstract = True

    upload_to_data = None

    job = None  # Just a placeholder for IDEs
    data = None  # Just a placeholder for IDEs

    if StrictVersion(django_version()) < StrictVersion('1.10.0'):
        # SEE: https://docs.djangoproject.com/en/1.10/topics/db/models/#field-name-hiding-is-not-permitted
        job = None  # Just a placeholder, Django < 1.10 does not support overriding Fields of abstract models
        data = None  # Just a placeholder, Django  < 1.10 does not support overriding Fields of abstract models
    else:
        job = models.ForeignKey(AJob, on_delete=models.CASCADE)  # placeholder, must be overridden by concrete class
        data = models.FileField(upload_to=job_data, max_length=256)
