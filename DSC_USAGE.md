# DSC (Digital Selective Calling) Implementation

## Övergripande

Du kan nu lyssna på VHF-kanal 70 och avkoda DSC-meddelanden! Systemet är integrerat direkt i scanner-loopen.

## Arkitektur

```
RTL-SDR (156.525 MHz)
    ↓
rtl_tcp server (port 1234)
    ↓
RTLReceiver (I/Q samples)
    ↓
Demodulation (GFSK → bits)
    ↓
DSCDecoder (frame extraction)
    ↓
DSCDirectReader (observations)
    ↓
Scanner loop (HybridBandScanner)
    ↓
API & Database
```

## Setupinstruktioner

### 1. RTL-SDR-hårdvara

Du behöver:
- RTL-SDR dongle (t.ex. RTL2832U)
- Anpassad antenn för 156.525 MHz (eller VHF-dipol)

### 2. rtl_tcp-server

Starta rtl_tcp-servern som lyssnar på lokal port:

```bash
rtl_tcp -a 127.0.0.1 -p 1234
```

Eller använd systemwide på port 1234 från din RTL-konfiguration.

### 3. Miljövariabel (.env)

Lägg till DSC-konfiguration i `.env`:

```env
# Enable DSC scanning (set to 0 to disable)
SDR_MONITOR_DSC_WINDOW_SECONDS=5.0

# RTL-SDR connection (rtl_tcp)
SDR_MONITOR_DSC_RTL_HOST=127.0.0.1
SDR_MONITOR_DSC_RTL_PORT=1234
SDR_MONITOR_DSC_RTL_SAMPLE_RATE=48000
SDR_MONITOR_DSC_RTL_GAIN=30
```

### 4. Starta SDR Monitor

```bash
cd sdr_monitor
python -m app.main
```

Scanner-loopen kommer nu att:
1. Lyssna på ADS-B (8s)
2. Pausa (2s)
3. Lyssna på AIS (12s)
4. Pausa (2s)
5. Lyssna på DSC (5s) ← **Nytt!**
6. Repetera

## Test

Utan RTL-SDR-hårdvara kan du testa dekodern med simulerad data:

```bash
python test_dsc.py
```

Output:

```
DSC DECODER TEST
[Test 1] Decoding DISTRESS (SINKING) message
  ✓ Decoded: MMSI=337117184, Type=DISTRESS
           Distress=N/A
           Position: (-64.0, -64.0)

[Test 2] Decoding URGENCY message
  ✓ Decoded: MMSI=71303168, Type=DISTRESS

[Test 3] Decoding MULTIPLE frames in sequence
  ✓ Decoded 3 messages
```

## API-endpoints

DSC-mål visas i standard API:

```bash
# Alla live-mål (inkl. DSC)
curl http://localhost:8000/api/targets/live | jq '.[] | select(.source == "dsc")'

# Statistik
curl http://localhost:8000/api/stats | jq '.sources | select(.dsc)'

# Historia för specifikt DSC-fartyg
curl http://localhost:8000/api/observations/dsc_123456789
```

## DSC-meddelandetyper

- **DISTRESS** (NÖDSIGNAL) - högsta prioritet
  - FIRE (brand)
  - FLOODING (läckage)
  - COLLISION (kollision)
  - GROUNDING (grund)
  - LISTING (slagning)
  - SINKING (sjunkande)
  - DISABLED_ADRIFT (motorstopp/drift)

- **URGENCY** (brådskande)
- **SAFETY** (säkerhet)
- **ROUTINE** (rutinmeddelande)
- **ALL_SHIPS** (alla fartyg)
- **GROUP_CALL** (gruppsamtal)

## Modulfiler

```
dsc_decoder/
  ├── dsc_decoder.py        # DSC-decoder (frame extraction & parsing)
  ├── rtl_receiver.py       # RTL-SDR I/Q mottagare
  ├── rtl_ais.py            # RTL-tcp wrapper (för kompatibilitet)
  └── __init__.py

sdr_monitor/
  └── app/
      ├── ingest_dsc.py     # DSC-ingestor för scanner
      ├── scanner.py        # Uppdaterad för DSC-band
      └── main.py           # Initialiserar DSC-reader
```

## Felsökning

### "DSC reader not available"
- Kontrollera att `dsc_decoder` moduler är installerade
- Kolla imports i `sdr_monitor/app/ingest_dsc.py`

### "Failed to connect to RTL-SDR"
- Starta `rtl_tcp` på port 1234
- Kontrollera `SDR_MONITOR_DSC_RTL_HOST` och `SDR_MONITOR_DSC_RTL_PORT`
- Kolla att RTL-SDR-dongeln är ansluten

### Ingen DSC-data
- Verifiera att `SDR_MONITOR_DSC_WINDOW_SECONDS > 0`
- Kontrollera att antennens frekvens är på 156.525 MHz
- Kolla log för dekoderfel

## Nästa steg

- [ ] Optimera demodulation för GFSK
- [ ] Lägg till mer robust framesynkronisering
- [ ] Stöd för positiondata (latitude/longitude)
- [ ] Distress-alarmer i UI
- [ ] JSON-export av DSC-historik
