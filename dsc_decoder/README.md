# DSC Decoder - VHF Channel 70 (156.525 MHz)

Python-baserad Digital Selective Calling (DSC) dekoderservice för maritim kommunikation.

## Komponenter

- **`dsc_decoder.py`**: Huvuddekoderlogik (GFSK-demodulering, DSC-ramavkodning)
- **`server.py`**: TCP-server som läser från RTL-SDR och skickar DSC-meddelanden
- **`simulator.py`**: Test-simulator för att testa utan hårdvara

## Funktioner

✅ GFSK-demodulering (1200 baud, ±600 Hz) från RTL-SDR I/Q-data
✅ DSC-ramavkodning med MMSI, meddelandetyp, nödtillstandstyp
✅ Positionsinformation (lat/lon)
✅ TCP-server för att distribuera meddelanden
✅ JSON-formaterade frames med 0x7E-avgränsare

## Installation

```bash
cd dsc_decoder
pip install -r requirements.txt
```

## Användning

### Med faktisk RTL-SDR-hårdvara

1. Starta `rtl_tcp` server på localhost:1234:
```bash
rtl_tcp -a 127.0.0.1 -p 1234
```

2. Starta DSC-dekodern:
```bash
python server.py --host 127.0.0.1 --port 6021 --rtl-host 127.0.0.1 --rtl-port 1234
```

3. Anslut SDR Monitor (redan konfigurerat för port 6021)

### Med simulator (för test)

```bash
# Testerna:
python simulator.py test

# Eller kör simulator + test client:
python simulator.py
```

## Integrering med SDR Monitor

DSC-dekodern är redan integrerad i SDR Monitor-systemet. Konfigurera `.env`:

```env
SDR_MONITOR_DSC_TCP_HOST=127.0.0.1
SDR_MONITOR_DSC_TCP_PORT=6021
SDR_MONITOR_DSC_WINDOW_SECONDS=5.0
```

## DSC-meddelandetyper

- **DISTRESS**: Nöd (högsta prioritet)
- **URGENCY**: Brådskande
- **SAFETY**: Säkerhet
- **ROUTINE**: Rutinmässigt samtal
- **ALL_SHIPS**: Utrop till alla fartyg
- **GROUP_CALL**: Gruppsamtal

## Nödtillståndstyper

- **FIRE**: Eldsvåda
- **FLOODING**: Vattentillströmning
- **COLLISION**: Kollision
- **GROUNDING**: Strandsättning
- **LISTING**: Krängning
- **SINKING**: Sjunkande fartyg
- **DISABLED_ADRIFT**: Motorbortfall/drivande

## Protokoll

### TCP Frame Format

Meddelanden skickas som JSON mellan 0x7E-avgränsare:

```
0x7E + {"timestamp": "...", "mmsi": ..., "message_type": "...", ...} + 0x7E
```

### Exempel

```json
{
  "timestamp": "2026-04-08T12:34:56+00:00",
  "mmsi": 123456789,
  "message_type": "DISTRESS",
  "distress_type": "SINKING",
  "latitude": 57.5,
  "longitude": 11.5,
  "raw": "7e1234567e..."
}
```

## Konfigurationsalternativ

```
usage: server.py [-h] [--rtl-host RTL_HOST] [--rtl-port RTL_PORT]
                 [--host HOST] [--port PORT]
                 [--sample-rate SAMPLE_RATE] [--gain GAIN]
                 [--log-level {DEBUG,INFO,WARNING,ERROR}]

options:
  --rtl-host RTL_HOST           RTL-SDR host (default: 127.0.0.1)
  --rtl-port RTL_PORT           RTL-SDR port (default: 1234)
  --host HOST                   DSC server bind host (default: 127.0.0.1)
  --port PORT                   DSC server bind port (default: 6021)
  --sample-rate SAMPLE_RATE     Sample rate (default: 48000)
  --gain GAIN                   RTL-SDR gain (default: 30)
  --log-level {DEBUG,...}       Logging level (default: INFO)
```

## Arkitektur

```
RTL-SDR (156.525 MHz)
    ↓
rtl_tcp (I/Q data stream)
    ↓
server.py (connects via rtl_tcp)
    ↓
GFSK_Demodulator (I/Q → bits)
    ↓
DSCDecoder (bits → frames)
    ↓
DSCMessage objects
    ↓
TCP clients (JSON frames)
    ↓
SDR Monitor (ingest_dsc.py)
```

## Tekniska detaljer

### DSC-specifikation
- **Frekvens**: VHF kanal 70 (156.525 MHz)
- **Baud-hastighet**: 1200 baud
- **Modulering**: GFSK (Gaussian Frequency Shift Keying)
- **Frekvensavvikelse**: ±600 Hz (mark/space)
- **Ramformat**: 20-25 bytes med synkflaggor (0x7E)
- **MMSI**: 30-bitars maritim identifierare

## Felsökning

### "Connection refused" från RTL-SDR
- Kontrollera att `rtl_tcp` körs på rätt värd/port
- Standardvärden: `127.0.0.1:1234`

### Ingen DSC-meddelanden avkodas
- Kontrollera loggar med `--log-level DEBUG`
- Verifiera att RTL-SDR är korrekt inställd på 156.525 MHz
- Kontrollera antennkvalitet och SDR-förstärkning

### Klienter ansluter men får ingen data
- Kontrollera att DSC-dekodern faktiskt mottar giltiga I/Q-data
- Se till att RTL-SDR-konfigurationen är korrekt (frekvens, sampel-hastighet)

## Future Enhancements

- [ ] Support för flera DSC-frekvenser (motsänd)
- [ ] Statistiksamling per MMSI
- [ ] DSC-signalkvalitetsmätning (SNR)
- [ ] Geo-spatial filtrering/alerting
- [ ] Integrering med AIS för tvärvalidering
