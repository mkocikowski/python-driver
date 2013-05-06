# TODO:
# - review locking, race conditions, deadlock
# - get values from proper config
# - proper threadpool submissions

import logging
import time
from threading import Lock, RLock, Condition
import weakref

from connection import MAX_STREAM_PER_CONNECTION, ConnectionException

log = logging.getLogger(__name__)


class BusyConnectionException(Exception):
    pass


class AuthenticationException(Exception):
    pass


class NoConnectionsAvailable(Exception):
    pass


class Host(object):

    address = None
    monitor = None

    _datacenter = None
    _rack = None
    _reconnection_handler = None

    def __init__(self, inet_address, conviction_policy_factory):
        if inet_address is None:
            raise ValueError("inet_address may not be None")
        if conviction_policy_factory is None:
            raise ValueError("conviction_policy_factory may not be None")

        self.address = inet_address
        self.monitor = HealthMonitor(conviction_policy_factory(self))

        self._reconnection_lock = Lock()

    @property
    def datacenter(self):
        return self._datacenter

    @property
    def rack(self):
        return self._rack

    def set_location_info(self, datacenter, rack):
        self._datacenter = datacenter
        self._rack = rack

    def get_and_set_reconnection_handler(self, new_handler):
        with self._reconnection_lock:
            old = self._reconnection_handler
            self._reconnection_handler = new_handler
            return old

    def __eq__(self, other):
        if not isinstance(other, Host):
            return False

        return self.address == other.address

    def __str__(self):
        return self.address

    def __repr__(self):
        return "<%s: %s>" % (self.__class__.__name__, self.address)


class _ReconnectionHandler(object):
    """
    Abstract class for attempting reconnections with a given
    schedule and scheduler.
    """

    _cancelled = False

    def __init__(self, scheduler, schedule, callback, *callback_args, **callback_kwargs):
        self.scheduler = scheduler
        self.schedule = schedule
        self.callback = callback
        self.callback_args = callback_args
        self.callback_kwargs = callback_kwargs

    def start(self):
        if self._cancelled:
            return

        # TODO cancel previous reconnection handlers? That's probably the job
        # of whatever created this.

        first_delay = self.schedule.next()
        self.scheduler.schedule(first_delay, self.run)

    def run(self):
        if self._cancelled:
            self.callback(*(self.callback_args), **(self.callback_kwargs))

        try:
            self.on_reconnection(self.try_reconnect())
        except Exception, exc:
            next_delay = self.schedule.next()
            if self.on_exception(exc, next_delay):
                self.scheduler.schedule(next_delay, self.run)
        else:
            self.callback(*(self.callback_args), **(self.callback_kwargs))

    def cancel(self):
        self._cancelled = True

    def try_reconnect(self):
        """
        Subclasses must implement this method.  It should attempt to
        open a new Connection and return it; if a failure occurs, an
        Exception should be raised.
        """
        raise NotImplemented()

    def on_reconnection(self, connection):
        """
        Called when a new Connection is successfully opened.  Nothing is
        done by default.
        """
        pass

    def on_exception(self, exc, next_delay):
        """
        Called when an Exception is raised when trying to connect.
        `exc` is the Exception that was raised and `next_delay` is the
        number of seconds (as a float) that the handler will wait before
        attempting to connect again.

        Subclasses should return ``False`` if no more attempts to connection
        should be made, ``True`` otherwise.  The default behavior is to
        always retry unless the error is an AuthenticationException.
        """
        if isinstance(exc, AuthenticationException):
            return False
        else:
            return True


class _HostReconnectionHandler(_ReconnectionHandler):

    def __init__(self, host, connection_factory, *args, **kwargs):
        _ReconnectionHandler.__init__(self, *args, **kwargs)
        self.host = host
        self.connection_factory = connection_factory

    def try_reconnect(self):
        return self.connection_factory(self.host.address)

    def on_reconnection(self, connection):
        self.host.monitor.reset()

    def on_exception(self, exc, next_delay):
        if isinstance(exc, AuthenticationException):
            return False
        else:
            log.exception("Error attempting to reconnect to %s" % (self.host,))
            return True


class HealthMonitor(object):

    is_up = True

    def __init__(self, conviction_policy):
        self._conviction_policy = conviction_policy
        self._host = conviction_policy.host
        # self._listeners will hold, among other things, references to
        # Cluster objects.  To allow those to be GC'ed (and shutdown) even
        # though we've implemented __del__, use weak references.
        self._listeners = weakref.WeakSet()
        self._lock = RLock()

    def register(self, listener):
        with self._lock:
            self._listeners.add(listener)

    def unregister(self, listener):
        with self._lock:
            self._listeners.remove(listener)

    def set_down(self):
        self.is_up = False

        with self._lock:
            listeners = self._listeners.copy()

        for listener in listeners:
            listener.on_down(self._host)

    def reset(self):
        self._conviction_policy.reset()

        with self._lock:
            listeners = self._listeners.copy()

        for listener in listeners:
            listener.on_up(self._host)

        self.is_up = True

    def signal_connection_failure(self, connection_exc):
        is_down = self._conviction_policy.add_failure(connection_exc)
        if is_down:
            self.set_down()
        return is_down


_MAX_SIMULTANEOUS_CREATION = 1


class HostConnectionPool(object):

    host = None
    host_distance = None

    is_shutdown = False
    open_count = 0
    _scheduled_for_creation = 0

    def __init__(self, host, host_distance, session):
        self.host = host
        self.host_distance = host_distance

        self._session = weakref.proxy(session)
        self._lock = RLock()
        self._conn_available_condition = Condition()

        core_conns = session.cluster.get_core_connections_per_host(host_distance)
        self._connections = [session.cluster.connection_factory(host.address)
                             for i in range(core_conns)]
        self._trash = set()
        self.open_count = core_conns

    def borrow_connection(self, timeout):
        if self.is_shutdown:
            raise ConnectionException(
                "Pool for %s is shutdown" % (self.host,), self.host)

        if not self._connections:
            # handled specially just for simpler code
            core_conns = self._session.cluster.get_core_connections_per_host(self.host_distance)
            with self._lock:
                for i in range(core_conns):
                    self._scheduled_for_creation += 1
                    self._session.submit(self._create_new_connection)

            # in_flight is incremented by wait_for_conn
            conn = self._wait_for_conn(timeout)
            conn.set_keyspace(self._session.keyspace)
            return conn
        else:
            least_busy = min(self._connections, key=lambda c: c.in_flight)
            max_reqs = self._session.cluster.get_max_requests_per_connection(self.host_distance)
            max_conns = self._session.cluster.get_max_connections_per_host(self.host_distance)

            # if we have too many requests on this connection but we still
            # have space to open a new connection against this host, go ahead
            # and schedule the creation of a new connection
            if least_busy.in_flight >= max_reqs and len(self._connections) < max_conns:
                self._maybe_spawn_new_connection()

            need_to_wait = False
            with least_busy.lock:
                if least_busy.in_flight >= MAX_STREAM_PER_CONNECTION:
                    # once we release the lock, wait for another connection
                    need_to_wait = True
                else:
                    least_busy.in_flight += 1

            if need_to_wait:
                # wait_for_conn will increment in_flight on the conn
                least_busy = self._wait_for_conn(timeout)

            least_busy.set_keyspace(self._session.keyspace)
            return least_busy

    def _maybe_spawn_new_connection(self):
        log.debug("Considering spawning new connection to %s" % (self.host.address,))
        with self._lock:
            if self._scheduled_for_creation >= _MAX_SIMULTANEOUS_CREATION:
                return
            self._scheduled_for_creation += 1

        self._session.submit(self._create_new_connection)

    def _create_new_connection(self):
        self._add_conn_if_under_max()
        with self._lock:
            self._scheduled_for_creation -= 1

    def _add_conn_if_under_max(self):
        max_conns = self._session.cluster.get_max_connections_per_host(self.host_distance)
        with self._lock:
            if self.is_shutdown:
                return False

            if self.open_count >= max_conns:
                return False

            self.open_count += 1

        try:
            conn = self._session.cluster.connection_factory(self.host)
            with self._lock:
                self._connections.append(conn)
            self._signal_available_conn()
        except ConnectionException, exc:
            with self._lock:
                self.open_count -= 1
            if self.host.monitor.signal_connection_failure(exc):
                self.shutdown()
            return False
        except AuthenticationException:
            with self._lock:
                self.open_count -= 1
            return False

    def _await_available_conn(self, timeout):
        with self._conn_available_condition:
            self._conn_available_condition.wait(timeout)

    def _signal_available_conn(self):
        with self._conn_available_condition:
            self._conn_available_condition.notify()

    def _signal_all_available_conn(self):
        with self._conn_available_condition:
            self._conn_available_condition.notify_all()

    def _wait_for_conn(self, timeout):
        start = time.time()
        remaining = timeout

        while remaining > 0:
            # wait on our condition for the possibility that a connection
            # is useable
            self._await_available_conn(remaining)

            # self.shutdown() may trigger the above Condition
            if self.is_shutdown:
                raise ConnectionException("Pool is shutdown")

            if self._connections:
                least_busy = min(self._connections, key=lambda c: c.in_flight)
                with least_busy.lock:
                    if least_busy.in_flight < MAX_STREAM_PER_CONNECTION:
                        least_busy.in_flight += 1
                        return least_busy

            remaining = timeout - (time.time() - start)

        raise NoConnectionsAvailable()

    def return_connection(self, connection):
        with connection.lock:
            connection.in_flight -= 1
            in_flight = connection.in_flight

        if connection.is_defunct:
            is_down = self.host.monitor.signal_connection_failure(connection.last_exception)
            if is_down:
                self.shutdown()
            else:
                self.replace(connection)
        else:
            with self._lock:
                # TODO another thread may have already taken this connection,
                # think about race condition here
                if connection in self._trash and in_flight == 0:
                    self._trash.remove(connection)
                    connection.close()
                    return

            core_conns = self._session.cluster.get_core_connections_per_host(self.host_distance)
            min_reqs = self._session.cluster.get_min_requests_per_connection(self.host_distance)
            if len(self._connections) > core_conns and in_flight <= min_reqs:
                self._trash_connection(connection)
            else:
                self._signal_available_conn()

    def _trash_connection(self, connection):
        core_conns = self._session.cluster.get_core_connections_per_host(self.host_distance)
        with self._lock:
            if self.open_count <= core_conns:
                return False

            self.open_count -= 1

            self._connections.remove(connection)
            with connection.lock:
                if connection.in_flight == 0:
                    connection.close()
                else:
                    self._trash.add(connection)

            return True

    def _replace(self, connection):
        with self._lock:
            self._connections.remove(connection)

        def close_and_replace():
            connection.close()
            self._add_conn_if_under_max()

        self._session.submit(close_and_replace)

    def _close(self, connection):
        self._session.submit(connection.close)

    def shutdown(self):
        with self._lock:
            if self.is_shutdown:
                return
            else:
                self.is_shutdown = True

        self._signal_all_available_conn()
        for conn in self._connections:
            conn.close()
            self.open_count -= 1

        reconnector = self.host.get_and_set_reconnection_handler(None)
        if reconnector:
            reconnector.cancel()

    def ensure_core_connections(self):
        if self.is_shutdown:
            return

        core_conns = self._session.cluster.get_core_connections_per_host(self.host_distance)
        for i in range(core_conns - self.open_count):
            with self._lock:
                self._scheduled_for_creation += 1
                self._session.submit(self._create_new_connection)
