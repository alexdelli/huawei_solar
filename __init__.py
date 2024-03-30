"""The Huawei Solar integration."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TypedDict, TypeVar

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.entity import DeviceInfo, Entity
from huawei_solar import (
    HuaweiSolarBridge,
    HuaweiSolarException,
    InvalidCredentials,
    register_values as rv,
)

from .const import (
    CONF_ENABLE_PARAMETER_CONFIGURATION,
    CONF_SLAVE_IDS,
    CONFIGURATION_UPDATE_INTERVAL,
    DATA_BRIDGES_WITH_DEVICEINFOS,
    DATA_UPDATE_COORDINATORS,
    DOMAIN,
    ENERGY_STORAGE_UPDATE_INTERVAL,
    INVERTER_UPDATE_INTERVAL,
    OPTIMIZER_UPDATE_INTERVAL,
    POWER_METER_UPDATE_INTERVAL,
)
from .services import async_cleanup_services, async_setup_services
from .update_coordinator import (
    HuaweiSolarOptimizerUpdateCoordinator,
    HuaweiSolarUpdateCoordinator,
    create_optimizer_update_coordinator,
)

_LOGGER = logging.getLogger(__name__)

T = TypeVar("T")

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.NUMBER,
    Platform.SWITCH,
    Platform.SELECT,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Huawei Solar from a config entry."""
    primary_bridge = None
    try:
        # Multiple inverters can be connected to each other via a daisy chain,
        # via an internal modbus-network (ie. not the same modbus network that we are
        # using to talk to the inverter).
        #
        # Each inverter receives it's own 'slave id' in that case.
        # The inverter that we use as 'gateway' will then forward the request to
        # the proper inverter.

        #               ┌─────────────┐
        #               │  EXTERNAL   │
        #               │ APPLICATION │
        #               └──────┬──────┘
        #                      │
        #                 ┌────┴────┐
        #                 │PRIMARY  │
        #                 │INVERTER │
        #                 └────┬────┘
        #       ┌──────────────┼───────────────┐
        #       │              │               │
        #  ┌────┴────┐     ┌───┴─────┐    ┌────┴────┐
        #  │ SLAVE X │     │ SLAVE Y │    │SLAVE ...│
        #  └─────────┘     └─────────┘    └─────────┘

        if entry.data[CONF_HOST] is None:
            primary_bridge = await HuaweiSolarBridge.create_rtu(
                port=entry.data[CONF_PORT], slave_id=entry.data[CONF_SLAVE_IDS][0]
            )
        else:
            primary_bridge = await HuaweiSolarBridge.create(
                host=entry.data[CONF_HOST],
                port=entry.data[CONF_PORT],
                slave_id=entry.data[CONF_SLAVE_IDS][0],
            )

            if entry.data.get(CONF_ENABLE_PARAMETER_CONFIGURATION):
                if entry.data.get(CONF_USERNAME) and entry.data.get(CONF_PASSWORD):
                    try:
                        await primary_bridge.login(
                            entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD]
                        )
                    except InvalidCredentials as err:
                        raise ConfigEntryAuthFailed() from err

        primary_bridge_device_infos = await compute_device_infos(
            primary_bridge,
            connecting_inverter_device_id=None,
        )

        bridges_with_device_infos: list[
            tuple[HuaweiSolarBridge, HuaweiInverterBridgeDeviceInfos]
        ] = [(primary_bridge, primary_bridge_device_infos)]

        for extra_slave_id in entry.data[CONF_SLAVE_IDS][1:]:
            extra_bridge = await HuaweiSolarBridge.create_extra_slave(
                primary_bridge, extra_slave_id
            )

            extra_bridge_device_infos = await compute_device_infos(
                extra_bridge,
                connecting_inverter_device_id=(
                    DOMAIN,
                    primary_bridge.serial_number,
                ),
            )

            bridges_with_device_infos.append((extra_bridge, extra_bridge_device_infos))

        # Now create update coordinators for each bridge
        update_coordinators: list[HuaweiSolarUpdateCoordinators] = []

        for bridge, device_infos in bridges_with_device_infos:
            inverter_update_coordinator = HuaweiSolarUpdateCoordinator(
                hass,
                _LOGGER,
                bridge=bridge,
                name=f"{bridge.serial_number}_inverter_data_update_coordinator",
                update_interval=INVERTER_UPDATE_INTERVAL,
            )

            power_meter_update_coordinator = None
            if bridge.power_meter_type:
                assert device_infos["power_meter"]
                power_meter_update_coordinator = HuaweiSolarUpdateCoordinator(
                    hass,
                    _LOGGER,
                    bridge=bridge,
                    name=f"{bridge.serial_number}_power_meter_data_update_coordinator",
                    update_interval=POWER_METER_UPDATE_INTERVAL,
                )

            energy_storage_update_coordinator = None
            if bridge.battery_type != rv.StorageProductModel.NONE:
                assert device_infos["connected_energy_storage"]
                energy_storage_update_coordinator = HuaweiSolarUpdateCoordinator(
                    hass,
                    _LOGGER,
                    bridge=bridge,
                    name=f"{bridge.serial_number}_battery_data_update_coordinator",
                    update_interval=ENERGY_STORAGE_UPDATE_INTERVAL,
                )

            configuration_update_coordinator = None
            if entry.data.get(CONF_ENABLE_PARAMETER_CONFIGURATION, False):
                configuration_update_coordinator = HuaweiSolarUpdateCoordinator(
                    hass,
                    _LOGGER,
                    bridge=bridge,
                    name=f"{bridge.serial_number}_config_data_update_coordinator",
                    update_interval=CONFIGURATION_UPDATE_INTERVAL,
                )

            optimizer_update_coordinator = None
            if bridge.has_optimizers:
                optimizers_device_infos = {}
                try:
                    optimizer_system_infos = (
                        await bridge.get_optimizer_system_information_data()
                    )
                    for optimizer_id, optimizer in optimizer_system_infos.items():
                        optimizers_device_infos[optimizer_id] = DeviceInfo(
                            identifiers={(DOMAIN, optimizer.sn)},
                            name=optimizer.sn,
                            manufacturer="Huawei",
                            model=optimizer.model,
                            sw_version=optimizer.software_version,
                            via_device=(DOMAIN, bridge.serial_number),
                        )

                    optimizer_update_coordinator = (
                        await create_optimizer_update_coordinator(
                            hass,
                            bridge,
                            optimizers_device_infos,
                            OPTIMIZER_UPDATE_INTERVAL,
                        )
                    )
                except HuaweiSolarException as exception:
                    _LOGGER.info(
                        "Cannot create optimizer sensor entities as the integration has insufficient permissions. "
                        "Consider enabling elevated permissions to get more optimizer data",
                        exc_info=exception,
                    )
                    optimizers_device_infos = {}
                except Exception as exc:  # pylint: disable=broad-except
                    _LOGGER.exception(
                        "Cannot create optimizer sensor entities due to an unexpected error",
                        exc_info=exc,
                    )
                    optimizers_device_infos = {}

            update_coordinators.append(
                HuaweiSolarUpdateCoordinators(
                    bridge=bridge,
                    device_infos=device_infos,
                    inverter_update_coordinator=inverter_update_coordinator,
                    power_meter_update_coordinator=power_meter_update_coordinator,
                    energy_storage_update_coordinator=energy_storage_update_coordinator,
                    optimizer_update_coordinator=optimizer_update_coordinator,
                    configuration_update_coordinator=configuration_update_coordinator,
                )
            )

        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
            DATA_BRIDGES_WITH_DEVICEINFOS: bridges_with_device_infos,
            DATA_UPDATE_COORDINATORS: update_coordinators,
        }
    except (HuaweiSolarException, TimeoutError) as err:
        if primary_bridge is not None:
            await primary_bridge.stop()

        raise ConfigEntryNotReady from err

    except Exception as err:
        # always try to stop the bridge, as it will keep retrying
        # in the background otherwise!
        if primary_bridge is not None:
            await primary_bridge.stop()

        raise err

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await async_setup_services(hass, entry)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        update_coordinators: list[HuaweiSolarUpdateCoordinators] = hass.data[DOMAIN][
            entry.entry_id
        ][DATA_UPDATE_COORDINATORS]
        for ucs in update_coordinators:
            await ucs.bridge.stop()

        await async_cleanup_services(hass)

        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


class HuaweiInverterBridgeDeviceInfos(TypedDict):
    """Device Infos for a specific inverter."""

    inverter: DeviceInfo
    power_meter: DeviceInfo | None
    connected_energy_storage: DeviceInfo | None


async def compute_device_infos(
    bridge: HuaweiSolarBridge,
    connecting_inverter_device_id: tuple[str, str] | None,
) -> HuaweiInverterBridgeDeviceInfos:
    """Create the correct DeviceInfo-objects, which can be used to correctly assign to entities in this integration."""
    inverter_device_info = DeviceInfo(
        identifiers={(DOMAIN, bridge.serial_number)},
        name="Inverter",
        manufacturer="Huawei",
        model=bridge.model_name,
        serial_number=bridge.serial_number,
        sw_version=bridge.software_version,
        via_device=connecting_inverter_device_id,  # type: ignore[typeddict-item]
    )

    # Add power meter device if a power meter is detected
    power_meter_device_info = None

    if bridge.power_meter_type is not None:
        power_meter_device_info = DeviceInfo(
            identifiers={
                (DOMAIN, f"{bridge.serial_number}/power_meter"),
            },
            name="Power meter",
            via_device=(DOMAIN, bridge.serial_number),
        )

    # Add battery device if a battery is detected
    battery_device_info = None

    if bridge.battery_type != rv.StorageProductModel.NONE:
        battery_device_info = DeviceInfo(
            identifiers={
                (DOMAIN, f"{bridge.serial_number}/connected_energy_storage"),
            },
            name="Battery",
            manufacturer=inverter_device_info.get("manufacturer"),
            model=f"{inverter_device_info.get('model')} Connected energy storage",
            via_device=(DOMAIN, bridge.serial_number),
        )

    return HuaweiInverterBridgeDeviceInfos(
        inverter=inverter_device_info,
        power_meter=power_meter_device_info,
        connected_energy_storage=battery_device_info,
    )


@dataclass
class HuaweiSolarUpdateCoordinators:
    """Device Infos for a specific inverter."""

    bridge: HuaweiSolarBridge
    device_infos: HuaweiInverterBridgeDeviceInfos

    inverter_update_coordinator: HuaweiSolarUpdateCoordinator
    power_meter_update_coordinator: HuaweiSolarUpdateCoordinator | None
    energy_storage_update_coordinator: HuaweiSolarUpdateCoordinator | None
    optimizer_update_coordinator: HuaweiSolarOptimizerUpdateCoordinator | None
    configuration_update_coordinator: HuaweiSolarUpdateCoordinator | None


class HuaweiSolarEntity(Entity):
    """Huawei Solar Entity."""

    _attr_has_entity_name = True
