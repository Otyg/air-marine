# DSC (Digital Selective Calling) Integration Guide

## Overview

This system now supports monitoring DSC (Digital Selective Calling) messages on VHF Channel 70 (156.525 MHz). DSC is a digital maritime safety and distress alerting system used for urgent and emergency calls on maritime VHF radio.

## DSC Capabilities

The DSC ingestor provides:

- **Message Type Classification**: Distress, Urgency, Safety, Routine, All-Ships, Group-Call
- **Distress Type Detection**: Fire, Flooding, Collision, Grounding, Listing, Sinking, Disabled/Adrift
- **Position Information**: Latitude/Longitude from DSC broadcasts
- **MMSI Identification**: Maritime Mobile Service Identity for vessel identification
- **Timestamp Recording**: UTC timestamps for all messages

## Architecture

The DSC implementation follows the same pattern as AIS and ADS-B:

- **DSCDecoder**: Low-level frame decoding and bit extraction
- **DSCTCPIngestor**: TCP client for reading from a DSC decoder service
- **Source Enum**: DSC added as a source type
- **ScanBand Enum**: DSC added as a scan band for the scanner loop

## Configuration

Add DSC configuration to your `.env` file:

```env
# DSC TCP decoder settings
SDR_MONITOR_DSC_TCP_HOST=127.0.0.1
SDR_MONITOR_DSC_TCP_PORT=6021

# DSC scan window (0 disables DSC monitoring)
SDR_MONITOR_DSC_WINDOW_SECONDS=5.0
```

### Configuration Variables

- `SDR_MONITOR_DSC_TCP_HOST`: Hostname/IP of DSC decoder (default: `127.0.0.1`)
- `SDR_MONITOR_DSC_TCP_PORT`: TCP port for DSC decoder (default: `6021`)
- `SDR_MONITOR_DSC_WINDOW_SECONDS`: DSC scan window duration in seconds (default: `0` = disabled)

## Setting Up a DSC Decoder

To use DSC functionality, you need a DSC decoder service that:

1. Monitors VHF Channel 70 (156.525 MHz)
2. Decodes DSC frames from the SDR input
3. Sends decoded messages via TCP to the configured host/port

Example options:

- **libdsc**: Python/C DSC decoder library
- **CSDR + DSC decoder**: Generic SDR pipeline
- **FLDIGI with scripting**: Ham radio software with DSC support
- **Custom decoder**: Implement your own using the expected frame format

### Frame Format

The DSC ingestor expects binary DSC frames with:

- **Preamble**: 32-bit sync pattern (0xAAAAAAAA)
- **Frame Flag**: 0x7E byte markers
- **Message Format**: 21-byte frames containing:
  - MMSI (30 bits)
  - Message Type (4 bits)
  - Distress Type (4 bits, if distress)
  - Latitude (27 bits, if present)
  - Longitude (28 bits, if present)

## Integration with Scanner

DSC is integrated into the hybrid band scanner:

```python
if self._config.dsc_window_seconds > 0:
    self._run_band_window(
        band=ScanBand.DSC,
        window_seconds=self._config.dsc_window_seconds,
        reader=self._dsc_reader,
        timeout_seconds=self._config.dsc_window_seconds,
        keep_decoder_running=False,
    )
```

When enabled, the scanner will alternate between AIS, ADS-B, and DSC bands based on configured window durations.

## Data Model

DSC messages are normalized to `NormalizedObservation`:

```python
{
    "source": "dsc",
    "target_id": "dsc_123456789",
    "target_kind": "vessel",
    "label": "MMSI: 123456789 DISTRESS (sinking)",
    "latitude": 57.5,
    "longitude": 11.5,
    "altitude": null,
    "speed_knots": null,
    "heading": null,
    "climb_rate_fpm": null,
    "timestamp": "2026-04-08T12:00:00+00:00",
    "raw": "7e1234567e..."
}
```

## Distress Handling

Distress messages are marked with `DISTRESS` in the label and include distress type:

- **FIRE**: Fire on vessel
- **FLOODING**: Water ingress
- **COLLISION**: Collision with another vessel/object
- **GROUNDING**: Vessel aground
- **LISTING**: Vessel tilting to one side
- **SINKING**: Vessel going down
- **DISABLED_ADRIFT**: Engine failure/drift
- **UNSPECIFIED**: Unknown distress type

Priority should be given to messages with `message_type=DISTRESS`.

## API Endpoints

DSC targets appear in standard API endpoints:

- `/api/targets/live` - Live targets including DSC vessels
- `/api/stats` - Scan statistics including DSC observations
- `/api/observations/:target_id` - History for specific DSC vessel

Example query:

```bash
curl http://localhost:8000/api/targets/live | jq '.[] | select(.source == "dsc")'
```

## Testing

Run DSC tests:

```bash
python -m pytest tests/test_ingest_dsc.py -v
```

Tests cover:

- Bit extraction and decoding
- Message type and distress type classification
- Latitude/longitude conversion
- Message to observation conversion
- TCP connection handling

## Troubleshooting

### No DSC messages appearing

1. Check DSC decoder service is running and listening on configured port:
   ```bash
   netstat -tlnp | grep 6021
   ```

2. Verify TCP connection:
   ```bash
   nc -zv 127.0.0.1 6021
   ```

3. Enable DEBUG logging:
   ```env
   SDR_MONITOR_LOG_LEVEL=DEBUG
   ```

4. Check firewall/iptables:
   ```bash
   sudo iptables -L -n | grep 6021
   ```

### Connection drops

- DSC decoder may be crashing; check logs
- Network issues; verify connectivity
- Increase `DSC_WINDOW_SECONDS` if timeouts are occurring
- Check socket buffer sizes if high packet loss

### Decoding errors

- Verify DSC decoder is sending valid binary frames
- Check frame alignment and sync pattern
- Enable frame logging in DSC decoder
- Validate frame structure against ITU-R M.493 specification

## References

- ITU-R Recommendation M.493-15: Digital Selective Calling System for Use in the Maritime Mobile Service
- IEC 62005: Maritime navigation and radiocommunication equipment and systems
- [ATIS DSC Specification](https://www.atis.org/)
