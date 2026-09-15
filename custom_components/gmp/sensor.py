"""The slow-moving numbers: credits, balance, last bill, last read.

Energy itself is not a sensor -- it is written to long-term statistics by the
coordinator, because GMP posts it a day late and a sensor would misstate when
it happened.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import GmpConfigEntry, GmpCoordinator

CURRENCY = "USD"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GmpConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensors for one GMP account."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            GmpCreditBalance(coordinator, entry),
            GmpAccountBalance(coordinator, entry),
            GmpLastBill(coordinator, entry),
            GmpLastRead(coordinator, entry),
        ]
    )


class GmpEntity(CoordinatorEntity[GmpCoordinator], SensorEntity):
    """Shared wiring: one device per GMP account."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: GmpCoordinator, entry: GmpConfigEntry, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.account_number}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.account_number)},
            name=entry.title,
            manufacturer="Green Mountain Power",
            model="Net metering account" if coordinator.data.has_generation else "Account",
            configuration_url="https://greenmountainpower.com/account",
        )


class GmpCreditBalance(GmpEntity):
    """Banked net-metering credit, in dollars."""

    _attr_name = "Net metering credits"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = CURRENCY

    def __init__(self, coordinator: GmpCoordinator, entry: GmpConfigEntry) -> None:
        super().__init__(coordinator, entry, "net_metering_credits")

    @property
    def native_value(self) -> float:
        """Total credit on the books.

        GMP stores credits as negative charges, so the portal negates the sum
        to show a positive balance; so does this.
        """
        return round(-sum(c.amount for c in self.coordinator.data.credits), 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Each credit with the date it expires, soonest first."""
        credits = self.coordinator.data.credits
        return {
            "next_expiration": credits[0].expiration_date.isoformat() if credits else None,
            "credits": [
                {
                    "credit_date": c.credit_date.isoformat(),
                    "expiration_date": c.expiration_date.isoformat(),
                    "amount": round(-c.amount, 2),
                }
                for c in credits
            ],
        }


class GmpAccountBalance(GmpEntity):
    """What GMP says is currently owed."""

    _attr_name = "Account balance"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = CURRENCY

    def __init__(self, coordinator: GmpCoordinator, entry: GmpConfigEntry) -> None:
        super().__init__(coordinator, entry, "account_balance")

    @property
    def native_value(self) -> float | None:
        """Current balance from the account status feed."""
        value = self.coordinator.data.status.get("currentBalance")
        return round(float(value), 2) if value is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Standing flags GMP reports alongside the balance."""
        status = self.coordinator.data.status
        return {
            "active": status.get("active"),
            "past_due": bool(status.get("pastDue30") or status.get("pastDue60")),
            "payoff_balance": status.get("payoffBalance"),
        }


class GmpLastBill(GmpEntity):
    """The most recent bill and the period it covered."""

    _attr_name = "Last bill"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = CURRENCY

    def __init__(self, coordinator: GmpCoordinator, entry: GmpConfigEntry) -> None:
        super().__init__(coordinator, entry, "last_bill")

    @property
    def native_value(self) -> float | None:
        """Amount of the last bill."""
        bill = self.coordinator.data.last_bill
        return round(bill.amount, 2) if bill else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """When it was issued and when its usage period began."""
        bill = self.coordinator.data.last_bill
        if bill is None:
            return {}
        return {
            "bill_date": bill.bill_date.isoformat(),
            "period_start": bill.period_start.date().isoformat(),
        }


class GmpLastRead(GmpEntity):
    """Start of the newest interval GMP has published -- how far behind we are."""

    _attr_name = "Latest usage data"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: GmpCoordinator, entry: GmpConfigEntry) -> None:
        super().__init__(coordinator, entry, "last_read")

    @property
    def native_value(self) -> datetime | None:
        """Start time of the newest read written to statistics."""
        return self.coordinator.data.last_read
