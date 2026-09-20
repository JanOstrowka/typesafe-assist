"""Build the "state" payload describing the home for a System One request."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from homeassistant.components.homeassistant.exposed_entities import (
    async_should_expose,
)
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import (
    area_registry as ar,
)
from homeassistant.helpers import (
    device_registry as dr,
)
from homeassistant.helpers import (
    entity_registry as er,
)
from homeassistant.helpers import (
    floor_registry as fr,
)
from homeassistant.helpers import (
    intent,
)

# Assistant id whose exposure settings apply (the Assist / conversation ones).
ASSISTANT = "conversation"

# Attributes worth telling the model about when include_state is enabled.
_INTERESTING_ATTRIBUTES = {
    "brightness",
    "current_position",
    "current_temperature",
    "temperature",
    "percentage",
    "volume_level",
    "device_class",
    "unit_of_measurement",
}

# Human descriptions of domains for the domain Choice question.
DOMAIN_DESCRIPTIONS: dict[str, str] = {
    "light": "Lights, lamps, bulbs, LED strips, lighting in general",
    "switch": "Switches, smart plugs, sockets, outlets, power strips, relays",
    "cover": "Blinds, shutters, curtains, shades, garage doors, gates",
    "climate": "Air conditioning, thermostat, heating, temperature setpoint",
    "fan": "Fans, air purifiers, ventilation",
    "media_player": "Speakers, TV, music, media playback, volume",
    "lock": "Door locks",
    "vacuum": "Robot vacuum",
    "humidifier": "Humidifier or dehumidifier",
    "scene": "A scene: a preset lighting/ambience configuration to activate",
    "script": "A script or routine to run",
    "automation": "An automation to enable or disable",
    "sensor": "A sensor reading such as temperature, humidity, air quality, battery",
    "binary_sensor": "A yes/no sensor such as motion, door/window contact, occupancy",
    "input_boolean": "A toggle / mode helper such as guest mode or sleep mode",
    "camera": "A camera",
    "valve": "A water or gas valve",
    "water_heater": "A water heater or boiler",
    "lawn_mower": "A robotic lawn mower",
    "todo": "A to-do or shopping list",
    "calendar": "A calendar",
    "weather": "Weather forecast",
    "timer": "A timer",
    "alarm_control_panel": "The alarm system",
    "siren": "A siren",
    "select": "A mode selector",
    "number": "A numeric setting",
    "button": "A push button",
    "update": "A firmware or software update",
}

DOMAIN_PLURALS: dict[str, str] = {
    "light": "lights",
    "switch": "switches",
    "cover": "covers",
    "climate": "thermostats",
    "fan": "fans",
    "media_player": "media players",
    "lock": "locks",
    "vacuum": "vacuums",
    "humidifier": "humidifiers",
    "scene": "scenes",
    "script": "scripts",
    "automation": "automations",
    "sensor": "sensors",
    "binary_sensor": "sensors",
    "input_boolean": "toggles",
    "valve": "valves",
}


@dataclass(slots=True)
class ExposedEntity:
    """An entity exposed to Assist."""

    entity_id: str
    domain: str
    name: str
    aliases: list[str]
    area: str | None
    floor: str | None
    state: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    available: bool = True

    @property
    def all_names(self) -> list[str]:
        """Primary name followed by aliases."""
        return [self.name, *self.aliases]

    def describe(self) -> str:
        """One-line description used as a Choice option rubric."""
        parts = [self.name]
        if self.aliases:
            parts.append(f"(also called: {', '.join(self.aliases)})")
        if self.area:
            parts.append(f"in the {self.area}")
        elif self.floor:
            parts.append(f"on the {self.floor} floor")
        if not self.available:
            parts.append("(currently unavailable)")
        return " ".join(parts)

    def as_state(self, include_state: bool) -> dict[str, Any]:
        """Representation for the request state payload."""
        data: dict[str, Any] = {
            "id": self.entity_id,
            "name": self.name,
            "type": self.domain,
        }
        if self.aliases:
            data["aliases"] = self.aliases
        if self.area:
            data["area"] = self.area
        if self.floor:
            data["floor"] = self.floor
        if include_state and self.state is not None:
            data["state"] = self.state
            if self.attributes:
                data["attributes"] = self.attributes
        return data


@dataclass(slots=True)
class HomeSnapshot:
    """Everything the model may need to know about the home."""

    entities: list[ExposedEntity]
    areas: dict[str, list[str]]  # area name -> aliases
    floors: dict[str, list[str]]  # floor name -> aliases
    device_area: str | None = None
    device_floor: str | None = None
    device_area_id: str | None = None
    device_floor_id: str | None = None

    @property
    def domains(self) -> list[str]:
        """Exposed domains, most populated first."""
        counts: dict[str, int] = {}
        for entity in self.entities:
            counts[entity.domain] = counts.get(entity.domain, 0) + 1
        return [d for d, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]

    def entities_in_domain(self, domain: str) -> list[ExposedEntity]:
        """Entities of one domain."""
        return [e for e in self.entities if e.domain == domain]

    def entity(self, entity_id: str) -> ExposedEntity | None:
        """Find an exposed entity by id."""
        for entity in self.entities:
            if entity.entity_id == entity_id:
                return entity
        return None

    def as_state(
        self, command: str, language: str, include_state: bool
    ) -> dict[str, Any]:
        """Build the request state payload."""
        state: dict[str, Any] = {
            "command": command,
            "language": language,
            "home": {
                "areas": [
                    {"name": name, **({"aliases": aliases} if aliases else {})}
                    for name, aliases in self.areas.items()
                ],
                "floors": list(self.floors),
                "devices": [e.as_state(include_state) for e in self.entities],
            },
        }
        if self.device_area:
            state["speaker_location"] = {
                "area": self.device_area,
                **({"floor": self.device_floor} if self.device_floor else {}),
                "note": "Commands that don't name a room usually refer to this area.",
            }
        return state


def _entity_names(
    hass: HomeAssistant, entry: er.RegistryEntry | None, state: State
) -> tuple[str, list[str]]:
    """Return (display name, other names) for an entity.

    Uses the same alias resolution as the intent matcher so that the names we
    show the model are exactly the ones Home Assistant would accept.
    """
    primary = state.name.strip() or state.entity_id
    names: list[str] = []
    if entry is not None:
        try:
            # 2026.x: aliases may contain a COMPUTED_NAME sentinel.
            names = intent.async_get_entity_aliases(
                hass, entry, state=state, allow_empty=False
            )
        except (AttributeError, TypeError):
            names = [alias for alias in entry.aliases if isinstance(alias, str)]
    seen = {primary.casefold()}
    aliases: list[str] = []
    for name in names:
        name = name.strip()
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            aliases.append(name)
    return primary, aliases


def _area_of_entry(
    entry: er.RegistryEntry | None,
    area_registry: ar.AreaRegistry,
    device_registry: dr.DeviceRegistry,
) -> ar.AreaEntry | None:
    """Area of an entity, falling back to its device's area."""
    if entry is None:
        return None
    if entry.area_id:
        return area_registry.async_get_area(entry.area_id)
    if entry.device_id:
        device = device_registry.async_get(entry.device_id)
        if device is not None and device.area_id:
            return area_registry.async_get_area(device.area_id)
    return None


@callback
def async_snapshot_home(
    hass: HomeAssistant,
    *,
    include_state: bool = False,
    device_id: str | None = None,
    satellite_id: str | None = None,
) -> HomeSnapshot:
    """Collect exposed entities, areas, floors and the speaker's location."""
    area_registry = ar.async_get(hass)
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    floor_registry = fr.async_get(hass)

    floors = {
        floor.name: list(floor.aliases)
        for floor in sorted(floor_registry.async_list_floors(), key=lambda f: f.name)
    }
    areas = {
        area.name: list(area.aliases)
        for area in sorted(area_registry.async_list_areas(), key=lambda a: a.name)
    }

    def floor_name_for_area(area: ar.AreaEntry | None) -> str | None:
        if area is None or area.floor_id is None:
            return None
        floor = floor_registry.async_get_floor(area.floor_id)
        return floor.name if floor else None

    entities: list[ExposedEntity] = []
    for state in sorted(hass.states.async_all(), key=lambda s: s.name):
        if not async_should_expose(hass, ASSISTANT, state.entity_id):
            continue
        entry = entity_registry.async_get(state.entity_id)
        area = _area_of_entry(entry, area_registry, device_registry)
        name, aliases = _entity_names(hass, entry, state)
        attributes: dict[str, Any] = {}
        if include_state:
            attributes = {
                key: (
                    str(value)
                    if not isinstance(value, (str, int, float, bool))
                    else value
                )
                for key, value in state.attributes.items()
                if key in _INTERESTING_ATTRIBUTES
            }
        entities.append(
            ExposedEntity(
                entity_id=state.entity_id,
                domain=state.domain,
                name=name,
                aliases=aliases,
                area=area.name if area else None,
                floor=floor_name_for_area(area),
                state=state.state if include_state else None,
                attributes=attributes,
                available=state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN),
            )
        )

    # Where is the speaker/device issuing the command?
    device_area: ar.AreaEntry | None = None
    if satellite_id:
        device_area = _area_of_entry(
            entity_registry.async_get(satellite_id), area_registry, device_registry
        )
    if device_area is None and device_id:
        device = device_registry.async_get(device_id)
        if device is not None and device.area_id:
            device_area = area_registry.async_get_area(device.area_id)

    device_floor_name = floor_name_for_area(device_area)

    return HomeSnapshot(
        entities=entities,
        areas=areas,
        floors=floors,
        device_area=device_area.name if device_area else None,
        device_floor=device_floor_name,
        device_area_id=device_area.id if device_area else None,
        device_floor_id=device_area.floor_id if device_area else None,
    )
