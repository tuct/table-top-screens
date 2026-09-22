"""SD card over SPI, sharing a bus that ESPHome already owns.

Both boards here need it, for different reasons:

* Seeed round display -- card shares the LCD's SPI bus, chip select on D2.
* Waveshare 4.3 -- card on free pins, but its chip select is on an I2C
  expander. That only makes sense for SPI mode with CS held asserted, and it
  is why SDMMC fails there: an SD card latches SPI-vs-SD mode from the CS
  level at its first CMD0 after power-up, and the expander idles LOW long
  before I2C is initialised. Use cs_pin: none and hold it low.


Why this exists: the available external component (n-serrette/esphome_sd_card)
drives the SDMMC peripheral, and SPI support is still an open request there
(issues #23, #25). The Seeed Round Display wires its card to the *same SPI
pins as the LCD* (SCK D8, MISO D9, MOSI D10, CS D2), so SDMMC cannot be used
without taking those pins away from the panel. SPI can: a bus is shared by
giving each device its own CS, which is exactly what Seeed's own example does
(`tft.init(); SD.begin(D2);`).

The trick is that ESP-IDF's esp_vfs_fat_sdspi_mount() deliberately does NOT
initialise the bus -- it attaches a device to an already-initialised one. That
is precisely our situation, since ESPHome's `spi:` component has already
called spi_bus_initialize(). So we only add a device.

ESPHome keeps SPIComponent::interface_ protected with no accessor, so the host
cannot be read back from the bus object; it is a config option instead,
defaulting to SPI2_HOST (what a single `spi:` gets on an ESP32-S3).
"""

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome import pins
from esphome.components.esp32 import include_builtin_idf_component
from esphome.const import CONF_CS_PIN, CONF_DATA_RATE, CONF_ID

CODEOWNERS = ["@tabletop_mini_screens"]
DEPENDENCIES = ["esp32", "spi"]

CONF_MOUNT_POINT = "mount_point"
CONF_SPI_HOST = "spi_host"
CONF_FORMAT_IF_MOUNT_FAILED = "format_if_mount_failed"
CONF_MAX_FILES = "max_files"

sd_spi_ns = cg.esphome_ns.namespace("sd_spi")
SdSpi = sd_spi_ns.class_("SdSpi", cg.Component)

# spi_host_device_t values. SPI1 is the flash bus and must never be used.
SPI_HOSTS = {"SPI2_HOST": 1, "SPI3_HOST": 2}


def _validate_mount_point(value):
    value = cv.string_strict(value)
    if not value.startswith("/"):
        raise cv.Invalid("mount_point must be an absolute path, e.g. /sd")
    if value.endswith("/") and value != "/":
        raise cv.Invalid("mount_point must not end with a slash")
    return value


CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.declare_id(SdSpi),
        # Raw pin number: handed to the IDF sdspi driver, which wants a
        # gpio_num_t rather than an ESPHome GPIOPin.
        #
        # "none" is legitimate: on a board whose chip select is not a GPIO at
        # all (an I2C expander, or tied low in hardware) the driver is told
        # SDSPI_SLOT_NO_CS and the board keeps CS asserted itself. Only safe
        # when the card is the sole device on the bus.
        cv.Optional(CONF_CS_PIN, default="none"): cv.Any(
            cv.one_of("none", lower=True), pins.internal_gpio_output_pin_number
        ),
        cv.Optional(CONF_SPI_HOST, default="SPI2_HOST"): cv.enum(
            SPI_HOSTS, upper=True
        ),
        cv.Optional(CONF_MOUNT_POINT, default="/sd"): _validate_mount_point,
        cv.Optional(CONF_FORMAT_IF_MOUNT_FAILED, default=False): cv.boolean,
        cv.Optional(CONF_MAX_FILES, default=4): cv.int_range(min=1, max=16),
        # SD cards in SPI mode are reliable to ~20 MHz on typical wiring; the
        # card is negotiated down automatically if it cannot keep up.
        cv.Optional(CONF_DATA_RATE, default="20MHz"): cv.All(
            cv.frequency, cv.int_range(min=400_000, max=40_000_000)
        ),
    }
).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    # Lets sd_clip compile with or without a card driver: a board whose card
    # is not on SPI (or has no slot) simply leaves this component out.
    cg.add_define("USE_SD_SPI")

    # FATFS is excluded from ESPHome builds by default to keep them small.
    include_builtin_idf_component("fatfs")

    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)

    cs = config[CONF_CS_PIN]
    cg.add(var.set_cs_pin(-1 if cs == "none" else cs))
    cg.add(var.set_spi_host(config[CONF_SPI_HOST]))
    cg.add(var.set_mount_point(config[CONF_MOUNT_POINT]))
    cg.add(var.set_format_if_mount_failed(config[CONF_FORMAT_IF_MOUNT_FAILED]))
    cg.add(var.set_max_files(config[CONF_MAX_FILES]))
    cg.add(var.set_max_freq_khz(int(config[CONF_DATA_RATE]) // 1000))
