from copy import copy, deepcopy
from collections import deque
from contextlib import contextmanager
from enum import Enum
from itertools import chain
import os
import sys
import uuid

from sentry_sdk.attachments import Attachment
from sentry_sdk._compat import datetime_utcnow
from sentry_sdk.consts import FALSE_VALUES, INSTRUMENTER
from sentry_sdk._functools import wraps
from sentry_sdk.profiler import Profile
from sentry_sdk.session import Session
from sentry_sdk.tracing_utils import (
    Baggage,
    extract_sentrytrace_data,
    has_tracing_enabled,
    normalize_incoming_data,
)
from sentry_sdk.tracing import (
    BAGGAGE_HEADER_NAME,
    SENTRY_TRACE_HEADER_NAME,
    NoOpSpan,
    Span,
    Transaction,
)
from sentry_sdk._types import TYPE_CHECKING
from sentry_sdk.utils import (
    capture_internal_exceptions,
    ContextVar,
    event_from_exception,
    exc_info_from_error,
    logger,
)

if TYPE_CHECKING:
    from typing import Any
    from typing import Callable
    from typing import Deque
    from typing import Dict
    from typing import Generator
    from typing import Iterator
    from typing import List
    from typing import Optional
    from typing import ParamSpec
    from typing import Tuple
    from typing import TypeVar
    from typing import Union

    from sentry_sdk._types import (
        Breadcrumb,
        BreadcrumbHint,
        ErrorProcessor,
        Event,
        EventProcessor,
        ExcInfo,
        Hint,
        Type,
    )

    import sentry_sdk

    P = ParamSpec("P")
    R = TypeVar("R")

    F = TypeVar("F", bound=Callable[..., Any])
    T = TypeVar("T")


_global_scope = None  # type: Optional[Scope]
_isolation_scope = ContextVar("isolation_scope", default=None)
_current_scope = ContextVar("current_scope", default=None)

global_event_processors = []  # type: List[EventProcessor]


class ScopeType(Enum):
    CURRENT = "current"
    ISOLATION = "isolation"
    GLOBAL = "global"
    MERGED = "merged"


def add_global_event_processor(processor):
    # type: (EventProcessor) -> None
    global_event_processors.append(processor)


def _attr_setter(fn):
    # type: (Any) -> Any
    return property(fset=fn, doc=fn.__doc__)


def _disable_capture(fn):
    # type: (F) -> F
    @wraps(fn)
    def wrapper(self, *args, **kwargs):
        # type: (Any, *Dict[str, Any], **Any) -> Any
        if not self._should_capture:
            return
        try:
            self._should_capture = False
            return fn(self, *args, **kwargs)
        finally:
            self._should_capture = True

    return wrapper  # type: ignore


def _copy_on_write(property_name):
    # type: (str) -> Callable[[Callable[P, R]], Callable[P, R]]
    """
    Decorator that implements copy-on-write on a property of the Scope.

    .. versionadded:: 1.XX.0
    """

    def decorator(func):
        # type: (Callable[P, R]) -> Callable[P, R]
        def wrapper(*args, **kwargs):
            # type: (*Any, **Any) -> Any
            self = args[0]
            same_property_different_scope = self.is_forked and id(
                getattr(self, property_name)
            ) == id(getattr(self.original_scope, property_name))

            if same_property_different_scope:
                setattr(
                    self,
                    property_name,
                    deepcopy(getattr(self.original_scope, property_name)),
                )

            return func(*args, **kwargs)

        return wrapper

    return decorator


class Scope(object):
    """The scope holds extra information that should be sent with all
    events that belong to it.
    """

    # NOTE: Even though it should not happen, the scope needs to not crash when
    # accessed by multiple threads. It's fine if it's full of races, but those
    # races should never make the user application crash.
    #
    # The same needs to hold for any accesses of the scope the SDK makes.

    __slots__ = (
        "_level",
        "_name",
        "_fingerprint",
        # note that for legacy reasons, _transaction is the transaction *name*,
        # not a Transaction object (the object is stored in _span)
        "_transaction",
        "_transaction_info",
        "_user",
        "_tags",
        "_contexts",
        "_extras",
        "_breadcrumbs",
        "_event_processors",
        "_error_processors",
        "_should_capture",
        "_span",
        "_session",
        "_attachments",
        "_force_auto_session_tracking",
        "_profile",
        "_propagation_context",
        "client",
        "original_scope",
        "_type",
    )

    def __init__(self, ty=None, client=None):
        # type: (Optional[ScopeType], Optional[sentry_sdk.Client]) -> None
        self._type = ty
        self.original_scope = None  # type: Optional[Scope]

        self._event_processors = []  # type: List[EventProcessor]
        self._error_processors = []  # type: List[ErrorProcessor]

        self._name = None  # type: Optional[str]
        self._propagation_context = None  # type: Optional[Dict[str, Any]]

        self.client = NoopClient()  # type: sentry_sdk.client.BaseClient

        if client is not None:
            self.set_client(client)

        self.clear()

        incoming_trace_information = self._load_trace_data_from_env()
        self.generate_propagation_context(incoming_data=incoming_trace_information)

    def __copy__(self):
        # type: () -> Scope
        """
        Returns a copy of this scope.
        This also creates a copy of all referenced data structures.
        """
        rv = object.__new__(self.__class__)  # type: Scope

        rv._level = self._level
        rv._name = self._name
        rv._fingerprint = self._fingerprint
        rv._transaction = self._transaction
        rv._transaction_info = dict(self._transaction_info)
        rv._user = self._user

        rv._tags = dict(self._tags)
        rv._contexts = dict(self._contexts)
        rv._extras = dict(self._extras)

        rv._breadcrumbs = copy(self._breadcrumbs)
        rv._event_processors = list(self._event_processors)
        rv._error_processors = list(self._error_processors)
        rv._propagation_context = self._propagation_context

        rv._should_capture = self._should_capture
        rv._span = self._span
        rv._session = self._session
        rv._force_auto_session_tracking = self._force_auto_session_tracking
        rv._attachments = list(self._attachments)

        rv._profile = self._profile

        return rv

    def _fork(self):
        # type: () -> Scope
        """
        Returns a fork of this scope.
        This creates a shallow copy of the scope and sets the original scope to this scope.

        This is our own implementation of a shallow copy because we have an existing __copy__() function
        what we will not tour for backward compatibility reasons.

        .. versionadded:: 1.XX.0
        """
        forked_scope = object.__new__(self.__class__)  # type: Scope

        forked_scope._level = self._level
        forked_scope._name = self._name
        forked_scope._fingerprint = self._fingerprint
        forked_scope._transaction = self._transaction
        forked_scope._transaction_info = self._transaction_info
        forked_scope._user = self._user

        forked_scope._tags = self._tags
        forked_scope._contexts = self._contexts
        forked_scope._extras = self._extras

        forked_scope._breadcrumbs = self._breadcrumbs
        forked_scope._event_processors = self._event_processors
        forked_scope._error_processors = self._error_processors
        forked_scope._propagation_context = self._propagation_context

        forked_scope._should_capture = self._should_capture
        forked_scope._span = self._span
        forked_scope._session = self._session
        forked_scope._force_auto_session_tracking = self._force_auto_session_tracking
        forked_scope._attachments = self._attachments

        forked_scope._profile = self._profile

        forked_scope.original_scope = self

        return forked_scope

    @classmethod
    def get_current_scope(cls):
        # type: () -> Scope
        """
        Returns the current scope.

        .. versionadded:: 1.XX.0
        """
        current_scope = _current_scope.get()
        if current_scope is None:
            current_scope = Scope(ty=ScopeType.CURRENT)
            _current_scope.set(current_scope)

        return current_scope

    @classmethod
    def get_isolation_scope(cls):
        # type: () -> Scope
        """
        Returns the isolation scope.

        .. versionadded:: 1.XX.0
        """
        isolation_scope = _isolation_scope.get()
        if isolation_scope is None:
            isolation_scope = Scope(ty=ScopeType.ISOLATION)
            _isolation_scope.set(isolation_scope)

        return isolation_scope

    @classmethod
    def get_global_scope(cls):
        # type: () -> Scope
        """
        Returns the global scope.

        .. versionadded:: 1.XX.0
        """
        global _global_scope
        if _global_scope is None:
            _global_scope = Scope(ty=ScopeType.GLOBAL)

        return _global_scope

    @classmethod
    def _merge_scopes(cls, additional_scope=None, additional_scope_kwargs=None):
        # type: (Optional[Scope], Optional[Dict[str, Any]]) -> Scope
        """
        Merges global, isolation and current scope into a new scope and
        adds the given additional scope or additional scope kwargs to it.

        .. versionadded:: 1.XX.0
        """
        if additional_scope and additional_scope_kwargs:
            raise TypeError("cannot provide scope and kwargs")

        final_scope = copy(_global_scope) if _global_scope is not None else Scope()
        final_scope._type = ScopeType.MERGED

        isolation_scope = _isolation_scope.get()
        if isolation_scope is not None:
            final_scope.update_from_scope(isolation_scope)

        current_scope = _current_scope.get()
        if current_scope is not None:
            final_scope.update_from_scope(current_scope)

        if additional_scope is not None:
            if callable(additional_scope):
                additional_scope(final_scope)
            else:
                final_scope.update_from_scope(additional_scope)

        elif additional_scope_kwargs:
            final_scope.update_from_kwargs(**additional_scope_kwargs)

        return final_scope

    @classmethod
    def get_client(cls):
        # type: () -> sentry_sdk.client.BaseClient
        """
        Returns the currently used :py:class:`sentry_sdk.Client`.
        This checks the current scope, the isolation scope and the global scope for a client.
        If no client is available a :py:class:`sentry_sdk.client.NoopClient` is returned.

        .. versionadded:: 1.XX.0
        """
        current_scope = _current_scope.get()
        if current_scope is not None and current_scope.client.is_active():
            return current_scope.client

        isolation_scope = _isolation_scope.get()
        if isolation_scope is not None and isolation_scope.client.is_active():
            return isolation_scope.client

        if _global_scope is not None:
            return _global_scope.client

        return NoopClient()

    def set_client(self, client=None):
        # type: (Optional[sentry_sdk.client.BaseClient]) -> None
        """
        Sets the client for this scope.
        :param client: The client to use in this scope.
            If `None` the client of the scope will be replaced by a :py:class:`sentry_sdk.NoopClient`.

        .. versionadded:: 1.XX.0
        """
        self.client = client or NoopClient()

    @property
    def is_forked(self):
        # type: () -> bool
        """
        Whether this scope is a fork of another scope.

        .. versionadded:: 1.XX.0
        """
        return self.original_scope is not None

    def fork(self):
        # type: () -> Scope
        """
        Returns a fork of this scope.

        .. versionadded:: 1.XX.0
        """
        return self._fork()

    def isolate(self):
        # type: () -> None
        """
        Creates a new isolation scope for this scope.
        The new isolation scope will be a fork of the current isolation scope.

        .. versionadded:: 1.XX.0
        """
        isolation_scope = Scope.get_isolation_scope()
        forked_isolation_scope = isolation_scope.fork()
        _isolation_scope.set(forked_isolation_scope)

    def _load_trace_data_from_env(self):
        # type: () -> Optional[Dict[str, str]]
        """
        Load Sentry trace id and baggage from environment variables.
        Can be disabled by setting SENTRY_USE_ENVIRONMENT to "false".
        """
        incoming_trace_information = None

        sentry_use_environment = (
            os.environ.get("SENTRY_USE_ENVIRONMENT") or ""
        ).lower()
        use_environment = sentry_use_environment not in FALSE_VALUES
        if use_environment:
            incoming_trace_information = {}

            if os.environ.get("SENTRY_TRACE"):
                incoming_trace_information[SENTRY_TRACE_HEADER_NAME] = (
                    os.environ.get("SENTRY_TRACE") or ""
                )

            if os.environ.get("SENTRY_BAGGAGE"):
                incoming_trace_information[BAGGAGE_HEADER_NAME] = (
                    os.environ.get("SENTRY_BAGGAGE") or ""
                )

        return incoming_trace_information or None

    def _extract_propagation_context(self, data):
        # type: (Dict[str, Any]) -> Optional[Dict[str, Any]]
        context = {}  # type: Dict[str, Any]
        normalized_data = normalize_incoming_data(data)

        baggage_header = normalized_data.get(BAGGAGE_HEADER_NAME)
        if baggage_header:
            context["dynamic_sampling_context"] = Baggage.from_incoming_header(
                baggage_header
            ).dynamic_sampling_context()

        sentry_trace_header = normalized_data.get(SENTRY_TRACE_HEADER_NAME)
        if sentry_trace_header:
            sentrytrace_data = extract_sentrytrace_data(sentry_trace_header)
            if sentrytrace_data is not None:
                context.update(sentrytrace_data)

        only_baggage_no_sentry_trace = (
            "dynamic_sampling_context" in context and "trace_id" not in context
        )
        if only_baggage_no_sentry_trace:
            context.update(self._create_new_propagation_context())

        if context:
            if not context.get("span_id"):
                context["span_id"] = uuid.uuid4().hex[16:]

            return context

        return None

    def _create_new_propagation_context(self):
        # type: () -> Dict[str, Any]
        return {
            "trace_id": uuid.uuid4().hex,
            "span_id": uuid.uuid4().hex[16:],
            "parent_span_id": None,
            "dynamic_sampling_context": None,
        }

    def set_new_propagation_context(self):
        # type: () -> None
        """
        Creates a new propagation context and sets it as `_propagation_context`. Overwriting existing one.
        """
        self._propagation_context = self._create_new_propagation_context()
        logger.debug(
            "[Tracing] Create new propagation context: %s",
            self._propagation_context,
        )

    def generate_propagation_context(self, incoming_data=None):
        # type: (Optional[Dict[str, str]]) -> None
        """
        Makes sure `_propagation_context` is set.
        If there is `incoming_data` overwrite existing `_propagation_context`.
        if there is no `incoming_data` create new `_propagation_context`, but do NOT overwrite if already existing.
        """
        if incoming_data:
            context = self._extract_propagation_context(incoming_data)

            if context is not None:
                self._propagation_context = context
                logger.debug(
                    "[Tracing] Extracted propagation context from incoming data: %s",
                    self._propagation_context,
                )

        if self._propagation_context is None and self._type != ScopeType.CURRENT:
            self.set_new_propagation_context()

    def get_dynamic_sampling_context(self):
        # type: () -> Optional[Dict[str, str]]
        """
        Returns the Dynamic Sampling Context from the Propagation Context.
        If not existing, creates a new one.
        """
        if self._propagation_context is None:
            return None

        baggage = self.get_baggage()
        if baggage is not None:
            self._propagation_context["dynamic_sampling_context"] = (
                baggage.dynamic_sampling_context()
            )

        return self._propagation_context["dynamic_sampling_context"]

    def get_traceparent(self, *args, **kwargs):
        # type: (Any, Any) -> Optional[str]
        """
        Returns the Sentry "sentry-trace" header (aka the traceparent) from the
        currently active span or the scopes Propagation Context.
        """
        client = Scope.get_client()

        # If we have an active span, return traceparent from there
        if has_tracing_enabled(client.options) and self.span is not None:
            return self.span.to_traceparent()

        if self._propagation_context is None:
            return None

        traceparent = "%s-%s" % (
            self._propagation_context["trace_id"],
            self._propagation_context["span_id"],
        )
        return traceparent

    def get_baggage(self, *args, **kwargs):
        # type: (Any, Any) -> Optional[Baggage]
        client = Scope.get_client()

        # If we have an active span, return baggage from there
        if has_tracing_enabled(client.options) and self.span is not None:
            return self.span.to_baggage()

        if self._propagation_context is None:
            return None

        dynamic_sampling_context = self._propagation_context.get(
            "dynamic_sampling_context"
        )
        if dynamic_sampling_context is None:
            return Baggage.from_options(self)
        else:
            return Baggage(dynamic_sampling_context)

    def get_trace_context(self):
        # type: () -> Any
        """
        Returns the Sentry "trace" context from the Propagation Context.
        """
        if self._propagation_context is None:
            return None

        trace_context = {
            "trace_id": self._propagation_context["trace_id"],
            "span_id": self._propagation_context["span_id"],
            "parent_span_id": self._propagation_context["parent_span_id"],
            "dynamic_sampling_context": self.get_dynamic_sampling_context(),
        }  # type: Dict[str, Any]

        return trace_context

    def trace_propagation_meta(self, *args, **kwargs):
        # type: (*Any, **Any) -> str
        """
        Return meta tags which should be injected into HTML templates
        to allow propagation of trace information.
        """
        span = kwargs.pop("span", None)
        if span is not None:
            logger.warning(
                "The parameter `span` in trace_propagation_meta() is deprecated and will be removed in the future."
            )

        meta = ""

        sentry_trace = self.get_traceparent()
        if sentry_trace is not None:
            meta += '<meta name="%s" content="%s">' % (
                SENTRY_TRACE_HEADER_NAME,
                sentry_trace,
            )

        baggage = self.get_baggage()
        if baggage is not None:
            meta += '<meta name="%s" content="%s">' % (
                BAGGAGE_HEADER_NAME,
                baggage.serialize(),
            )

        return meta

    def iter_headers(self):
        # type: () -> Iterator[Tuple[str, str]]
        """
        Creates a generator which returns the `sentry-trace` and `baggage` headers from the Propagation Context.
        """
        if self._propagation_context is not None:
            traceparent = self.get_traceparent()
            if traceparent is not None:
                yield SENTRY_TRACE_HEADER_NAME, traceparent

            dsc = self.get_dynamic_sampling_context()
            if dsc is not None:
                baggage = Baggage(dsc).serialize()
                yield BAGGAGE_HEADER_NAME, baggage

    def iter_trace_propagation_headers(self, *args, **kwargs):
        # type: (Any, Any) -> Generator[Tuple[str, str], None, None]
        """
        Return HTTP headers which allow propagation of trace data. Data taken
        from the span representing the request, if available, or the current
        span on the scope if not.
        """
        client = Scope.get_client()
        if not client.options.get("propagate_traces"):
            return

        span = kwargs.pop("span", None)
        span = span or self.span

        if has_tracing_enabled(client.options) and span is not None:
            for header in span.iter_headers():
                yield header
        else:
            for header in self.iter_headers():
                yield header

    def clear(self):
        # type: () -> None
        """Clears the entire scope."""
        self._level = None  # type: Optional[str]
        self._fingerprint = None  # type: Optional[List[str]]
        self._transaction = None  # type: Optional[str]
        self._transaction_info = {}  # type: Dict[str, str]
        self._user = None  # type: Optional[Dict[str, Any]]

        self._tags = {}  # type: Dict[str, Any]
        self._contexts = {}  # type: Dict[str, Dict[str, Any]]
        self._extras = {}  # type: Dict[str, Any]
        self._attachments = []  # type: List[Attachment]

        self.clear_breadcrumbs()
        self._should_capture = True

        self._span = None  # type: Optional[Span]
        self._session = None  # type: Optional[Session]
        self._force_auto_session_tracking = None  # type: Optional[bool]

        self._profile = None  # type: Optional[Profile]

        self._propagation_context = None

    @_attr_setter
    def level(self, value):
        # type: (Optional[str]) -> None
        """When set this overrides the level. Deprecated in favor of set_level."""
        self._level = value

    def set_level(self, value):
        # type: (Optional[str]) -> None
        """Sets the level for the scope."""
        self._level = value

    @_attr_setter
    def fingerprint(self, value):
        # type: (Optional[List[str]]) -> None
        """When set this overrides the default fingerprint."""
        self._fingerprint = value

    @property
    def transaction(self):
        # type: () -> Any
        # would be type: () -> Optional[Transaction], see https://github.com/python/mypy/issues/3004
        """Return the transaction (root span) in the scope, if any."""

        # there is no span/transaction on the scope
        if self._span is None:
            return None

        # there is an orphan span on the scope
        if self._span.containing_transaction is None:
            return None

        # there is either a transaction (which is its own containing
        # transaction) or a non-orphan span on the scope
        return self._span.containing_transaction

    @transaction.setter
    def transaction(self, value):
        # type: (Any) -> None
        # would be type: (Optional[str]) -> None, see https://github.com/python/mypy/issues/3004
        """When set this forces a specific transaction name to be set.

        Deprecated: use set_transaction_name instead."""

        # XXX: the docstring above is misleading. The implementation of
        # apply_to_event prefers an existing value of event.transaction over
        # anything set in the scope.
        # XXX: note that with the introduction of the Scope.transaction getter,
        # there is a semantic and type mismatch between getter and setter. The
        # getter returns a Transaction, the setter sets a transaction name.
        # Without breaking version compatibility, we could make the setter set a
        # transaction name or transaction (self._span) depending on the type of
        # the value argument.

        logger.warning(
            "Assigning to scope.transaction directly is deprecated: use scope.set_transaction_name() instead."
        )
        self._transaction = value
        if self._span and self._span.containing_transaction:
            self._span.containing_transaction.name = value

    def set_transaction_name(self, name, source=None):
        # type: (str, Optional[str]) -> None
        """Set the transaction name and optionally the transaction source."""
        self._transaction = name

        if self._span and self._span.containing_transaction:
            self._span.containing_transaction.name = name
            if source:
                self._span.containing_transaction.source = source

        if source:
            self._transaction_info["source"] = source

    @_attr_setter
    def user(self, value):
        # type: (Optional[Dict[str, Any]]) -> None
        """When set a specific user is bound to the scope. Deprecated in favor of set_user."""
        self.set_user(value)

    def set_user(self, value):
        # type: (Optional[Dict[str, Any]]) -> None
        """Sets a user for the scope."""
        self._user = value
        session = Scope.get_isolation_scope()._session
        if session is not None:
            session.update(user=value)

    @property
    def span(self):
        # type: () -> Optional[Span]
        """Get/set current tracing span or transaction."""
        return self._span

    @span.setter
    def span(self, span):
        # type: (Optional[Span]) -> None
        self._span = span
        # XXX: this differs from the implementation in JS, there Scope.setSpan
        # does not set Scope._transactionName.
        if isinstance(span, Transaction):
            transaction = span
            if transaction.name:
                self._transaction = transaction.name
                if transaction.source:
                    self._transaction_info["source"] = transaction.source

    @property
    def profile(self):
        # type: () -> Optional[Profile]
        return self._profile

    @profile.setter
    def profile(self, profile):
        # type: (Optional[Profile]) -> None

        self._profile = profile

    @_copy_on_write("_tags")
    def set_tag(
        self,
        key,  # type: str
        value,  # type: Any
    ):
        # type: (...) -> None
        """Sets a tag for a key to a specific value."""
        self._tags[key] = value

    @_copy_on_write("_tags")
    def remove_tag(
        self, key  # type: str
    ):
        # type: (...) -> None
        """Removes a specific tag."""
        self._tags.pop(key, None)

    def set_context(
        self,
        key,  # type: str
        value,  # type: Dict[str, Any]
    ):
        # type: (...) -> None
        """Binds a context at a certain key to a specific value."""
        self._contexts[key] = value

    def remove_context(
        self, key  # type: str
    ):
        # type: (...) -> None
        """Removes a context."""
        self._contexts.pop(key, None)

    def set_extra(
        self,
        key,  # type: str
        value,  # type: Any
    ):
        # type: (...) -> None
        """Sets an extra key to a specific value."""
        self._extras[key] = value

    def remove_extra(
        self, key  # type: str
    ):
        # type: (...) -> None
        """Removes a specific extra key."""
        self._extras.pop(key, None)

    def clear_breadcrumbs(self):
        # type: () -> None
        """Clears breadcrumb buffer."""
        self._breadcrumbs = deque()  # type: Deque[Breadcrumb]

    def add_attachment(
        self,
        bytes=None,  # type: Optional[bytes]
        filename=None,  # type: Optional[str]
        path=None,  # type: Optional[str]
        content_type=None,  # type: Optional[str]
        add_to_transactions=False,  # type: bool
    ):
        # type: (...) -> None
        """Adds an attachment to future events sent."""
        self._attachments.append(
            Attachment(
                bytes=bytes,
                path=path,
                filename=filename,
                content_type=content_type,
                add_to_transactions=add_to_transactions,
            )
        )

    def add_breadcrumb(self, crumb=None, hint=None, **kwargs):
        # type: (Optional[Breadcrumb], Optional[BreadcrumbHint], Any) -> None
        """
        Adds a breadcrumb.

        :param crumb: Dictionary with the data as the sentry v7/v8 protocol expects.

        :param hint: An optional value that can be used by `before_breadcrumb`
            to customize the breadcrumbs that are emitted.
        """
        client = Scope.get_client()

        if not client.is_active():
            logger.info("Dropped breadcrumb because no client bound")
            return

        before_breadcrumb = client.options["before_breadcrumb"]
        max_breadcrumbs = client.options["max_breadcrumbs"]

        crumb = dict(crumb or ())  # type: Breadcrumb
        crumb.update(kwargs)
        if not crumb:
            return

        hint = dict(hint or ())  # type: Hint

        if crumb.get("timestamp") is None:
            crumb["timestamp"] = datetime_utcnow()
        if crumb.get("type") is None:
            crumb["type"] = "default"

        if before_breadcrumb is not None:
            new_crumb = before_breadcrumb(crumb, hint)
        else:
            new_crumb = crumb

        if new_crumb is not None:
            self._breadcrumbs.append(new_crumb)
        else:
            logger.info("before breadcrumb dropped breadcrumb (%s)", crumb)

        while len(self._breadcrumbs) > max_breadcrumbs:
            self._breadcrumbs.popleft()

    def start_transaction(
        self, transaction=None, instrumenter=INSTRUMENTER.SENTRY, **kwargs
    ):
        # type: (Optional[Transaction], str, Any) -> Union[Transaction, NoOpSpan]
        """
        Start and return a transaction.

        Start an existing transaction if given, otherwise create and start a new
        transaction with kwargs.

        This is the entry point to manual tracing instrumentation.

        A tree structure can be built by adding child spans to the transaction,
        and child spans to other spans. To start a new child span within the
        transaction or any span, call the respective `.start_child()` method.

        Every child span must be finished before the transaction is finished,
        otherwise the unfinished spans are discarded.

        When used as context managers, spans and transactions are automatically
        finished at the end of the `with` block. If not using context managers,
        call the `.finish()` method.

        When the transaction is finished, it will be sent to Sentry with all its
        finished child spans.

        For supported `**kwargs` see :py:class:`sentry_sdk.tracing.Transaction`.
        """
        client = Scope.get_client()

        configuration_instrumenter = client.options["instrumenter"]

        if instrumenter != configuration_instrumenter:
            return NoOpSpan()

        custom_sampling_context = kwargs.pop("custom_sampling_context", {})

        # if we haven't been given a transaction, make one
        if transaction is None:
            transaction = Transaction(**kwargs)

        # use traces_sample_rate, traces_sampler, and/or inheritance to make a
        # sampling decision
        sampling_context = {
            "transaction_context": transaction.to_json(),
            "parent_sampled": transaction.parent_sampled,
        }
        sampling_context.update(custom_sampling_context)
        transaction._set_initial_sampling_decision(sampling_context=sampling_context)

        profile = Profile(transaction)
        profile._set_initial_sampling_decision(sampling_context=sampling_context)

        # we don't bother to keep spans if we already know we're not going to
        # send the transaction
        if transaction.sampled:
            max_spans = (client.options["_experiments"].get("max_spans")) or 1000
            transaction.init_span_recorder(maxlen=max_spans)

        return transaction

    def start_span(self, span=None, instrumenter=INSTRUMENTER.SENTRY, **kwargs):
        # type: (Optional[Span], str, Any) -> Span
        """
        Start a span whose parent is the currently active span or transaction, if any.

        The return value is a :py:class:`sentry_sdk.tracing.Span` instance,
        typically used as a context manager to start and stop timing in a `with`
        block.

        Only spans contained in a transaction are sent to Sentry. Most
        integrations start a transaction at the appropriate time, for example
        for every incoming HTTP request. Use
        :py:meth:`sentry_sdk.start_transaction` to start a new transaction when
        one is not already in progress.

        For supported `**kwargs` see :py:class:`sentry_sdk.tracing.Span`.
        """
        client = Scope.get_client()

        configuration_instrumenter = client.options["instrumenter"]

        if instrumenter != configuration_instrumenter:
            return NoOpSpan()

        # THIS BLOCK IS DEPRECATED
        # TODO: consider removing this in a future release.
        # This is for backwards compatibility with releases before
        # start_transaction existed, to allow for a smoother transition.
        if isinstance(span, Transaction) or "transaction" in kwargs:
            deprecation_msg = (
                "Deprecated: use start_transaction to start transactions and "
                "Transaction.start_child to start spans."
            )

            if isinstance(span, Transaction):
                logger.warning(deprecation_msg)
                return self.start_transaction(span, **kwargs)

            if "transaction" in kwargs:
                logger.warning(deprecation_msg)
                name = kwargs.pop("transaction")
                return self.start_transaction(name=name, **kwargs)

        # THIS BLOCK IS DEPRECATED
        # We do not pass a span into start_span in our code base, so I deprecate this.
        if span is not None:
            deprecation_msg = "Deprecated: passing a span into `start_span` is deprecated and will be removed in the future."
            logger.warning(deprecation_msg)
            return span

        active_span = self.span
        if active_span is not None:
            new_child_span = active_span.start_child(**kwargs)
            return new_child_span

        # If there is already a trace_id in the propagation context, use it.
        # This does not need to be done for `start_child` above because it takes
        # the trace_id from the parent span.
        if "trace_id" not in kwargs:
            traceparent = self.get_traceparent()
            trace_id = traceparent.split("-")[0] if traceparent else None
            if trace_id is not None:
                kwargs["trace_id"] = trace_id

        return Span(**kwargs)

    def continue_trace(self, environ_or_headers, op=None, name=None, source=None):
        # type: (Dict[str, Any], Optional[str], Optional[str], Optional[str]) -> Transaction
        """
        Sets the propagation context from environment or headers and returns a transaction.
        """
        self.generate_propagation_context(environ_or_headers)

        transaction = Transaction.continue_from_headers(
            normalize_incoming_data(environ_or_headers),
            op=op,
            name=name,
            source=source,
        )

        return transaction

    def capture_event(self, event, hint=None, scope=None, **scope_kwargs):
        # type: (Event, Optional[Hint], Optional[Scope], Any) -> Optional[str]
        """
        Captures an event.

        Merges given scope data and calls :py:meth:`sentry_sdk.Client.capture_event`.

        :param event: A ready-made event that can be directly sent to Sentry.

        :param hint: Contains metadata about the event that can be read from `before_send`, such as the original exception object or a HTTP request object.

        :param scope: An optional :py:class:`sentry_sdk.Scope` to apply to events.
            The `scope` and `scope_kwargs` parameters are mutually exclusive.

        :param scope_kwargs: Optional data to apply to event.
            For supported `**scope_kwargs` see :py:meth:`sentry_sdk.Scope.update_from_kwargs`.
            The `scope` and `scope_kwargs` parameters are mutually exclusive.

        :returns: An `event_id` if the SDK decided to send the event (see :py:meth:`sentry_sdk.Client.capture_event`).
        """
        scope = Scope._merge_scopes(scope, scope_kwargs)

        return Scope.get_client().capture_event(event=event, hint=hint, scope=scope)

    def capture_message(self, message, level=None, scope=None, **scope_kwargs):
        # type: (str, Optional[str], Optional[Scope], Any) -> Optional[str]
        """
        Captures a message.

        :param message: The string to send as the message.

        :param level: If no level is provided, the default level is `info`.

        :param scope: An optional :py:class:`sentry_sdk.Scope` to apply to events.
            The `scope` and `scope_kwargs` parameters are mutually exclusive.

        :param scope_kwargs: Optional data to apply to event.
            For supported `**scope_kwargs` see :py:meth:`sentry_sdk.Scope.update_from_kwargs`.
            The `scope` and `scope_kwargs` parameters are mutually exclusive.

        :returns: An `event_id` if the SDK decided to send the event (see :py:meth:`sentry_sdk.Client.capture_event`).
        """
        if level is None:
            level = "info"

        event = {
            "message": message,
            "level": level,
        }

        return self.capture_event(event, scope=scope, **scope_kwargs)

    def capture_exception(self, error=None, scope=None, **scope_kwargs):
        # type: (Optional[Union[BaseException, ExcInfo]], Optional[Scope], Any) -> Optional[str]
        """Captures an exception.

        :param error: An exception to capture. If `None`, `sys.exc_info()` will be used.

        :param scope: An optional :py:class:`sentry_sdk.Scope` to apply to events.
            The `scope` and `scope_kwargs` parameters are mutually exclusive.

        :param scope_kwargs: Optional data to apply to event.
            For supported `**scope_kwargs` see :py:meth:`sentry_sdk.Scope.update_from_kwargs`.
            The `scope` and `scope_kwargs` parameters are mutually exclusive.

        :returns: An `event_id` if the SDK decided to send the event (see :py:meth:`sentry_sdk.Client.capture_event`).
        """
        if error is not None:
            exc_info = exc_info_from_error(error)
        else:
            exc_info = sys.exc_info()

        event, hint = event_from_exception(
            exc_info, client_options=Scope.get_client().options
        )

        try:
            return self.capture_event(event, hint=hint, scope=scope, **scope_kwargs)
        except Exception:
            self._capture_internal_exception(sys.exc_info())

        return None

    def _capture_internal_exception(
        self, exc_info  # type: Any
    ):
        # type: (...) -> Any
        """
        Capture an exception that is likely caused by a bug in the SDK
        itself.

        These exceptions do not end up in Sentry and are just logged instead.
        """
        logger.error("Internal error in sentry_sdk", exc_info=exc_info)

    def start_session(self, *args, **kwargs):
        # type: (*Any, **Any) -> None
        """Starts a new session."""
        session_mode = kwargs.pop("session_mode", "application")

        self.end_session()

        client = Scope.get_client()
        self._session = Session(
            release=client.options["release"],
            environment=client.options["environment"],
            user=self._user,
            session_mode=session_mode,
        )

    def end_session(self, *args, **kwargs):
        # type: (*Any, **Any) -> None
        """Ends the current session if there is one."""
        session = self._session
        self._session = None

        if session is not None:
            session.close()
            Scope.get_client().capture_session(session)

    def stop_auto_session_tracking(self, *args, **kwargs):
        # type: (*Any, **Any) -> None
        """Stops automatic session tracking.

        This temporarily session tracking for the current scope when called.
        To resume session tracking call `resume_auto_session_tracking`.
        """
        self.end_session()
        self._force_auto_session_tracking = False

    def resume_auto_session_tracking(self):
        # type: (...) -> None
        """Resumes automatic session tracking for the current scope if
        disabled earlier.  This requires that generally automatic session
        tracking is enabled.
        """
        self._force_auto_session_tracking = None

    def add_event_processor(
        self, func  # type: EventProcessor
    ):
        # type: (...) -> None
        """Register a scope local event processor on the scope.

        :param func: This function behaves like `before_send.`
        """
        if len(self._event_processors) > 20:
            logger.warning(
                "Too many event processors on scope! Clearing list to free up some memory: %r",
                self._event_processors,
            )
            del self._event_processors[:]

        self._event_processors.append(func)

    def add_error_processor(
        self,
        func,  # type: ErrorProcessor
        cls=None,  # type: Optional[Type[BaseException]]
    ):
        # type: (...) -> None
        """Register a scope local error processor on the scope.

        :param func: A callback that works similar to an event processor but is invoked with the original exception info triple as second argument.

        :param cls: Optionally, only process exceptions of this type.
        """
        if cls is not None:
            cls_ = cls  # For mypy.
            real_func = func

            def func(event, exc_info):
                # type: (Event, ExcInfo) -> Optional[Event]
                try:
                    is_inst = isinstance(exc_info[1], cls_)
                except Exception:
                    is_inst = False
                if is_inst:
                    return real_func(event, exc_info)
                return event

        self._error_processors.append(func)

    def _apply_level_to_event(self, event, hint, options):
        # type: (Event, Hint, Optional[Dict[str, Any]]) -> None
        if self._level is not None:
            event["level"] = self._level

    def _apply_breadcrumbs_to_event(self, event, hint, options):
        # type: (Event, Hint, Optional[Dict[str, Any]]) -> None
        event.setdefault("breadcrumbs", {}).setdefault("values", []).extend(
            self._breadcrumbs
        )

    def _apply_user_to_event(self, event, hint, options):
        # type: (Event, Hint, Optional[Dict[str, Any]]) -> None
        if event.get("user") is None and self._user is not None:
            event["user"] = self._user

    def _apply_transaction_name_to_event(self, event, hint, options):
        # type: (Event, Hint, Optional[Dict[str, Any]]) -> None
        if event.get("transaction") is None and self._transaction is not None:
            event["transaction"] = self._transaction

    def _apply_transaction_info_to_event(self, event, hint, options):
        # type: (Event, Hint, Optional[Dict[str, Any]]) -> None
        if event.get("transaction_info") is None and self._transaction_info is not None:
            event["transaction_info"] = self._transaction_info

    def _apply_fingerprint_to_event(self, event, hint, options):
        # type: (Event, Hint, Optional[Dict[str, Any]]) -> None
        if event.get("fingerprint") is None and self._fingerprint is not None:
            event["fingerprint"] = self._fingerprint

    def _apply_extra_to_event(self, event, hint, options):
        # type: (Event, Hint, Optional[Dict[str, Any]]) -> None
        if self._extras:
            event.setdefault("extra", {}).update(self._extras)

    def _apply_tags_to_event(self, event, hint, options):
        # type: (Event, Hint, Optional[Dict[str, Any]]) -> None
        if self._tags:
            event.setdefault("tags", {}).update(self._tags)

    def _apply_contexts_to_event(self, event, hint, options):
        # type: (Event, Hint, Optional[Dict[str, Any]]) -> None
        if self._contexts:
            event.setdefault("contexts", {}).update(self._contexts)

        contexts = event.setdefault("contexts", {})

        # Add "trace" context
        if contexts.get("trace") is None:
            if has_tracing_enabled(options) and self._span is not None:
                contexts["trace"] = self._span.get_trace_context()
            else:
                contexts["trace"] = self.get_trace_context()

        # Add "reply_id" context
        try:
            replay_id = contexts["trace"]["dynamic_sampling_context"]["replay_id"]
        except (KeyError, TypeError):
            replay_id = None

        if replay_id is not None:
            contexts["replay"] = {
                "replay_id": replay_id,
            }

    def _drop(self, cause, ty):
        # type: (Any, str) -> Optional[Any]
        logger.info("%s (%s) dropped event", ty, cause)
        return None

    def run_error_processors(self, event, hint):
        # type: (Event, Hint) -> Optional[Event]
        """
        Runs the error processors on the event and returns the modified event.
        """
        exc_info = hint.get("exc_info")
        if exc_info is not None:
            error_processors = chain(
                Scope.get_global_scope()._error_processors,
                Scope.get_isolation_scope()._error_processors,
                Scope.get_current_scope()._error_processors,
            )

            for error_processor in error_processors:
                new_event = error_processor(event, exc_info)
                if new_event is None:
                    return self._drop(error_processor, "error processor")

                event = new_event

        return event

    def run_event_processors(self, event, hint):
        # type: (Event, Hint) -> Optional[Event]
        """
        Runs the event processors on the event and returns the modified event.
        """
        ty = event.get("type")
        is_check_in = ty == "check_in"

        if not is_check_in:
            global _global_scope
            isolation_scope = _isolation_scope.get()
            current_scope = _current_scope.get()
            event_processors = chain(
                global_event_processors,
                _global_scope and _global_scope._event_processors or [],
                isolation_scope and isolation_scope._event_processors or [],
                current_scope and current_scope._event_processors or [],
            )

            for event_processor in event_processors:
                new_event = event
                with capture_internal_exceptions():
                    new_event = event_processor(event, hint)
                if new_event is None:
                    return self._drop(event_processor, "event processor")
                event = new_event

        return event

    @_disable_capture
    def apply_to_event(
        self,
        event,  # type: Event
        hint,  # type: Hint
        options=None,  # type: Optional[Dict[str, Any]]
    ):
        # type: (...) -> Optional[Event]
        """Applies the information contained on the scope to the given event."""
        ty = event.get("type")
        is_transaction = ty == "transaction"
        is_check_in = ty == "check_in"

        # put all attachments into the hint. This lets callbacks play around
        # with attachments. We also later pull this out of the hint when we
        # create the envelope.
        attachments_to_send = hint.get("attachments") or []
        for attachment in self._attachments:
            if not is_transaction or attachment.add_to_transactions:
                attachments_to_send.append(attachment)
        hint["attachments"] = attachments_to_send

        self._apply_contexts_to_event(event, hint, options)

        if is_check_in:
            # Check-ins only support the trace context, strip all others
            event["contexts"] = {
                "trace": event.setdefault("contexts", {}).get("trace", {})
            }

        if not is_check_in:
            self._apply_level_to_event(event, hint, options)
            self._apply_fingerprint_to_event(event, hint, options)
            self._apply_user_to_event(event, hint, options)
            self._apply_transaction_name_to_event(event, hint, options)
            self._apply_transaction_info_to_event(event, hint, options)
            self._apply_tags_to_event(event, hint, options)
            self._apply_extra_to_event(event, hint, options)

        if not is_transaction and not is_check_in:
            self._apply_breadcrumbs_to_event(event, hint, options)

        event = self.run_error_processors(event, hint)
        if event is None:
            return None

        event = self.run_event_processors(event, hint)
        if event is None:
            return None

        return event

    def update_from_scope(self, scope):
        # type: (Scope) -> None
        """Update the scope with another scope's data."""
        if scope._level is not None:
            self._level = scope._level
        if scope._fingerprint is not None:
            self._fingerprint = scope._fingerprint
        if scope._transaction is not None:
            self._transaction = scope._transaction
        if scope._transaction_info is not None:
            self._transaction_info.update(scope._transaction_info)
        if scope._user is not None:
            self._user = scope._user
        if scope._tags:
            self._tags.update(scope._tags)
        if scope._contexts:
            self._contexts.update(scope._contexts)
        if scope._extras:
            self._extras.update(scope._extras)
        if scope._breadcrumbs:
            self._breadcrumbs.extend(scope._breadcrumbs)
        if scope._span:
            self._span = scope._span
        if scope._attachments:
            self._attachments.extend(scope._attachments)
        if scope._profile:
            self._profile = scope._profile
        if scope._propagation_context:
            self._propagation_context = scope._propagation_context
        if scope._session:
            self._session = scope._session

    def update_from_kwargs(
        self,
        user=None,  # type: Optional[Any]
        level=None,  # type: Optional[str]
        extras=None,  # type: Optional[Dict[str, Any]]
        contexts=None,  # type: Optional[Dict[str, Any]]
        tags=None,  # type: Optional[Dict[str, str]]
        fingerprint=None,  # type: Optional[List[str]]
    ):
        # type: (...) -> None
        """Update the scope's attributes."""
        if level is not None:
            self._level = level
        if user is not None:
            self._user = user
        if extras is not None:
            self._extras.update(extras)
        if contexts is not None:
            self._contexts.update(contexts)
        if tags is not None:
            self._tags.update(tags)
        if fingerprint is not None:
            self._fingerprint = fingerprint

    def __repr__(self):
        # type: () -> str
        return "<%s id=%s name=%s>" % (
            self.__class__.__name__,
            hex(id(self)),
            self._name,
        )


@contextmanager
def new_scope():
    # type: () -> Generator[Scope, None, None]
    """
    Context manager that forks the current scope and runs the wrapped code in it.

    .. versionadded:: 1.XX.0
    """
    current_scope = Scope.get_current_scope()
    forked_scope = current_scope.fork()
    token = _current_scope.set(forked_scope)

    try:
        yield forked_scope

    finally:
        # restore original scope
        _current_scope.reset(token)


@contextmanager
def isolated_scope():
    # type: () -> Generator[Scope, None, None]
    """
    Context manager that forks the current isolation scope
    (and the related current scope) and runs the wrapped code in it.

    .. versionadded:: 1.XX.0
    """
    # fork current scope
    current_scope = Scope.get_current_scope()
    forked_current_scope = current_scope.fork()
    current_token = _current_scope.set(forked_current_scope)

    # fork isolation scope
    isolation_scope = Scope.get_isolation_scope()
    forked_isolation_scope = isolation_scope.fork()
    isolation_token = _isolation_scope.set(forked_isolation_scope)

    try:
        yield forked_isolation_scope

    finally:
        # restore original scopes
        _current_scope.reset(current_token)
        _isolation_scope.reset(isolation_token)


# Circular imports
from sentry_sdk.client import NoopClient
