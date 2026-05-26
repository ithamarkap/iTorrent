"""
upnp_manager.py -- UPnP automatic port-forwarding for iTorrent.

Uses the `miniupnpc` library to discover the router's Internet Gateway
Device (IGD) and add a TCP port mapping so that remote peers can reach us for
seeding, even behind NAT.
"""

import logging
import socket
import threading
import time

logger = logging.getLogger('UPnP')

# How long (seconds) before we renew the mapping to keep it alive.
_LEASE_SECONDS = 3600
_RENEW_BEFORE   = 60


class UPnPManager:
    """Manages a single TCP UPnP port mapping for the BitTorrent listen port."""

    def __init__(self):
        self._lock          = threading.Lock()
        self._enabled       = True          # toggled by the GUI
        self._active        = False         # True while a mapping is held
        self._port          = None          # currently mapped port
        self._external_ip   = None
        self._upnp          = None          # cached miniupnpc.UPnP instance
        self._renew_timer   = None

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def external_ip(self) -> str | None:
        return self._external_ip

    @property
    def enabled(self) -> bool:
        return self._enabled

    def setup(self, port: int):
        """
        Discover the router and add a port mapping for *port* (TCP).
        Runs in a background thread so it never blocks the caller.
        """
        threading.Thread(target=self._setup_worker, args=(port,),
                         daemon=True, name='UPnP-setup').start()

    def teardown(self):
        """Remove the current port mapping (if any) and cancel the renewal timer."""
        with self._lock:
            self._cancel_renew()
            if self._active and self._port is not None:
                self._delete_mapping(self._port)
            self._active = False
            self._port   = None
            self._external_ip = None

    def set_enabled(self, enabled: bool, listen_port: int = 6881):
        """
        Toggle UPnP on or off from the GUI.
        """
        with self._lock:
            self._enabled = enabled

        if enabled:
            logger.info('[UPnP] Enabled -- setting up mapping.')
            self.setup(listen_port)
        else:
            logger.info('[UPnP] Disabled -- removing mapping.')
            self.teardown()

    def get_status(self) -> dict:
        """Return a JSON-serialisable status dict for the GUI."""
        return {
            'enabled':    self._enabled,
            'active':     self._active,
            'externalIp': self._external_ip,
            'port':       self._port,
        }

    # ── Internal ──────────────────────────────────────────────────────────────

    def _setup_worker(self, port: int):
        with self._lock:
            if not self._enabled:
                logger.info('[UPnP] Skipping setup -- UPnP is disabled.')
                return

            if self._active and self._port == port:
                logger.debug('[UPnP] Mapping already active for port %d.', port)
                return
            if self._active:
                self._cancel_renew()
                self._delete_mapping(self._port)
                self._active = False

        try:
            import miniupnpc
        except ImportError:
            logger.warning('[UPnP] miniupnpc is not installed -- run: pip install miniupnpc')
            return

        try:
            logger.info('[UPnP] Discovering Internet Gateway Device...')
            upnp = miniupnpc.UPnP()
            upnp.discoverdelay = 200
            
            # discover() returns the number of devices found
            ndevices = upnp.discover()
            if ndevices == 0:
                logger.warning('[UPnP] No UPnP devices found.')
                return
            
            # selectigd() chooses the best IGD found (the router)
            upnp.selectigd()
            
            # get the router's local IP address
            local_ip = upnp.lanaddr
            # get the router's external IP address
            external_ip = upnp.externalipaddress()
            
            # Add port mapping (TCP)
            # addportmapping(external_port, protocol, internal_host, internal_port, description, remote_host)
            upnp.addportmapping(port, 'TCP', local_ip, port, 'iTorrent', '')

            with self._lock:
                self._upnp        = upnp
                self._active      = True
                self._port        = port
                self._external_ip = external_ip

            logger.info(
                '[UPnP] OK: Mapped external %s:%d -> %s:%d (TCP)',
                external_ip, port, local_ip, port,
            )

            # Schedule renewal
            self._schedule_renew(port, local_ip)

        except Exception as e:
            logger.warning('[UPnP] Setup failed: %s', e)

    def _delete_mapping(self, port: int):
        if self._upnp is None or port is None:
            return
        try:
            self._upnp.deleteportmapping(port, 'TCP')
            logger.info('[UPnP] Removed mapping for port %d.', port)
        except Exception as e:
            logger.debug('[UPnP] deleteportmapping failed: %s', e)

    def _schedule_renew(self, port: int, local_ip: str):
        delay = max(_LEASE_SECONDS - _RENEW_BEFORE, 30)

        def _renew():
            logger.info('[UPnP] Renewing port mapping for port %d...', port)
            try:
                self._upnp.addportmapping(port, 'TCP', local_ip, port, 'iTorrent', '')
                logger.info('[UPnP] Mapping renewed for port %d.', port)
            except Exception as e:
                logger.warning('[UPnP] Renewal failed: %s', e)
            
            with self._lock:
                if self._active and self._enabled:
                    self._schedule_renew(port, local_ip)

        with self._lock:
            self._cancel_renew()
            t = threading.Timer(delay, _renew)
            t.daemon = True
            t.name   = 'UPnP-renew'
            t.start()
            self._renew_timer = t

    def _cancel_renew(self):
        if self._renew_timer is not None:
            self._renew_timer.cancel()
            self._renew_timer = None


# Module-level singleton -- imported by gui/app.py
upnp_manager = UPnPManager()
