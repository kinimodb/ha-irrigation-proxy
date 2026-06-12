"""Shared base class for all Irrigation Proxy entities."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_NAME, DOMAIN
from .coordinator import IrrigationCoordinator


class IrrigationProxyEntity(CoordinatorEntity[IrrigationCoordinator]):
    """Gemeinsame Basis: Coordinator-Anbindung + Geräte-Gruppierung.

    Alle Entities eines Config-Entries hängen am selben virtuellen Gerät,
    damit sie in der HA-UI als ein Controller erscheinen.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: IrrigationCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.data.get(CONF_NAME, "Irrigation"),
            manufacturer="Irrigation Proxy",
            model="Virtual Irrigation Controller",
        )
