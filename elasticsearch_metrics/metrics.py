from django.conf import settings
from django.apps import apps
from django.utils import timezone
from django.utils.six import add_metaclass
from elasticsearch_dsl import Document, Date
from elasticsearch_dsl.document import IndexMeta, MetaField

from elasticsearch_metrics.signals import pre_index_template_create, pre_save, post_save
from elasticsearch_metrics.registry import registry


class MetricMeta(IndexMeta):
    """Metaclass for the base `Metric` class."""

    def __new__(mcls, name, bases, attrs):  # noqa: B902

        meta = attrs.get("Meta", None)
        module = attrs.get("__module__")

        new_cls = super(MetricMeta, mcls).__new__(mcls, name, bases, attrs)
        # Also ensure initialization is only performed for subclasses of Metric
        # (excluding Metric class itself).
        if not any(
            b for b in bases if isinstance(b, MetricMeta) and b is not BaseMetric
        ):
            return new_cls

        template_name = getattr(meta, "template_name", None)
        template = getattr(meta, "template", None)
        abstract = getattr(meta, "abstract", False)

        app_label = getattr(meta, "app_label", None)
        # Look for an application configuration to attach the model to.
        app_config = apps.get_containing_app_config(module)
        if app_label is None:
            if app_config is None:
                if not abstract:
                    raise RuntimeError(
                        "Metric class %s.%s doesn't declare an explicit "
                        "app_label and isn't in an application in "
                        "INSTALLED_APPS." % (module, name)
                    )
            else:
                app_label = app_config.label

        if not template_name or not template:
            metric_name = new_cls.__name__.lower()
            # If template_name not specified in class Meta,
            # compute it as <app label>_<lowercased class name>
            if not template_name:
                template_name = "{}_{}".format(app_label, metric_name)
            # template is <app label>_<lowercased class name>-*
            template = template or "{}_{}-*".format(app_label, metric_name)

        new_cls._template_name = template_name
        new_cls._template = template
        # Abstract base metrics can't be instantiated and don't appear in
        # the list of metrics for an app.
        if not abstract:
            registry.register(app_label, new_cls)
        return new_cls


# We need this intermediate BaseMetric class so that
# we can run MetricMeta ahead of IndexMeta
@add_metaclass(MetricMeta)
class BaseMetric(object):
    """Base metric class with which to define custom metric classes.

    Example usage:

    .. code-block:: python

        from elasticsearch_metrics.metrics import Metric

        class PageView(Metric):
            user_id = Integer()

            class Index:
                settings = {
                    "number_of_shards": 2,
                    "refresh_interval": "5s",
                }
    """

    timestamp = Date(doc_values=True)

    class Meta:
        all = MetaField(enabled=False)
        source = MetaField(enabled=False)

    @classmethod
    def create_index_template(cls, using=None):
        """Create an index template for this metric in Elasticsearch."""
        index_template = cls.get_index_template()
        index_template.document(cls)
        pre_index_template_create.send(cls, index_template=index_template, using=using)
        index_template.save(using=using)
        return index_template

    @classmethod
    def get_index_template(cls):
        """Return an `IndexTemplate <elasticsearch_dsl.IndexTemplate>` for this metric."""
        return cls._index.as_template(
            template_name=cls._template_name, pattern=cls._template
        )

    @classmethod
    def get_index_name(cls, date=None):
        date = date or timezone.now().date()
        dateformat = settings.DATE_FORMAT
        date_formatted = date.strftime(dateformat)
        return "{}-{}".format(cls._template_name, date_formatted)


class Metric(Document, BaseMetric):
    __doc__ = BaseMetric.__doc__

    @classmethod
    def init(cls, index=None, using=None):
        """
        Create the index and populate the mappings in elasticsearch.
        """
        return super(Metric, cls).init(index=index or cls.get_index_name(), using=using)

    def save(self, using=None, index=None, validate=True, **kwargs):
        self.timestamp = timezone.now()
        if not index:
            index = self.get_index_name()

        cls = self.__class__
        pre_save.send(cls, instance=self, using=using, index=index)
        ret = super(Metric, self).save(
            using=using, index=index, validate=validate, **kwargs
        )
        post_save.send(cls, instance=self, using=using, index=index)
        return ret