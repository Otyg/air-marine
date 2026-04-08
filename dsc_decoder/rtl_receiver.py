#!/usr/bin/env python3
"""
RTL-SDR I/Q receiver for DSC decoding.

Connects to rtl_tcp (or compatible RTL-SDR server), receives I/Q samples,
and feeds them to the DSC decoder.
"""

import struct
import socket
import math
import logging
from typing import Optional
from collections import deque

logger = logging.getLogger(__name__)

# DSC parameters
DSC_FREQUENCY = 156.525e6  # VHF Channel 70
DSC_BAUD_RATE = 1200
DSC_DEVIATION = 50  # Hz typical deviation for DSC


class RTLReceiver:
    """
    Minimal RTL-SDR receiver interface for DSC.
    
    Connects to rtl_tcp and provides I/Q sample stream demodulation.
    """
    
    def __init__(
        self,
        frequency: float = DSC_FREQUENCY,
        sample_rate: int = 48000,
        gain: int = 30,
        host: str = "127.0.0.1",
        port: int = 1234,
    ):
        """
        Initialize RTL receiver.
        
        Args:
            frequency: Target frequency in Hz (default: 156.525 MHz)
            sample_rate: Sample rate in Hz (default: 48000 Hz)
            gain: Receiver gain in dB (default: 30 dB)
            host: rtl_tcp host
            port: rtl_tcp port
        """
        self.frequency = frequency
        self.sample_rate = sample_rate
        self.gain = gain
        self.host = host
        self.port = port
        self.socket: Optional[socket.socket] = None
        self.connected = False
        
        # I/Q buffer for processing
        self._iq_buffer = deque(maxlen=self.sample_rate // 10)  # 100ms buffer
        
    def connect(self) -> bool:
        """Connect to rtl_tcp server"""
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.connect((self.host, self.port))
            self.connected = True
            
            # Send rtl_tcp commands
            self._set_frequency(int(self.frequency))
            self._set_sample_rate(self.sample_rate)
            self._set_gain(self.gain)
            
            logger.info(f"Connected to RTL-SDR at {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to RTL-SDR: {e}")
            self.connected = False
            return False
    
    def disconnect(self):
        """Disconnect from rtl_tcp"""
        if self.socket:
            self.socket.close()
            self.connected = False
    
    def read_samples(self, num_samples: int = 1024) -> Optional[bytes]:
        """
        Read raw I/Q samples from rtl_tcp.
        
        Args:
            num_samples: Number of I/Q pairs to read
            
        Returns:
            Bytes of raw I/Q data (2 bytes per sample: I then Q)
        """
        if not self.connected or not self.socket:
            return None
        
        try:
            # Each sample is 2 bytes (I and Q as uint8)
            data = self.socket.recv(num_samples * 2)
            return data if data else None
        except Exception as e:
            logger.error(f"Error reading samples: {e}")
            self.connected = False
            return None
    
    def demodulate_to_bits(self, iq_data: bytes, chunks: int = 8) -> bytes:
        """
        Demodulate I/Q samples to bits using simple GFSK demodulation.
        
        This is a basic approach suitable for DSC:
        1. Convert I/Q to magnitude and phase
        2. Extract frequency deviation
        3. Threshold to binary
        
        Args:
            iq_data: Raw I/Q bytes (alternating I, Q)
            chunks: Samples per output bit (determines baud rate)
            
        Returns:
            Demodulated bits as bytes
        """
        bits = bytearray()
        
        # Parse I/Q samples
        iq_samples = []
        for i in range(0, len(iq_data) - 1, 2):
            i_val = int(iq_data[i]) - 128  # Convert to signed
            q_val = int(iq_data[i + 1]) - 128
            iq_samples.append((i_val, q_val))
        
        # Simple frequency discriminator
        # Calculate phase difference between consecutive samples
        phases = []
        for i in range(1, len(iq_samples)):
            i_curr, q_curr = iq_samples[i]
            i_prev, q_prev = iq_samples[i - 1]
            
            # Phase: atan2(q, i)
            phase_curr = math.atan2(q_curr, i_curr)
            phase_prev = math.atan2(q_prev, i_prev)
            
            # Phase difference
            phase_diff = phase_curr - phase_prev
            
            # Normalize to [-pi, pi]
            if phase_diff > math.pi:
                phase_diff -= 2 * math.pi
            elif phase_diff < -math.pi:
                phase_diff += 2 * math.pi
            
            phases.append(phase_diff)
        
        # Convert phase differences to bits using threshold
        for i in range(0, len(phases) - chunks, chunks):
            phase_chunk = sum(phases[i:i + chunks]) / chunks
            # Positive phase = +1 (mark), negative = 0 (space)
            bit = 1 if phase_chunk > 0 else 0
            bits.append(bit)
        
        return bytes(bits)
    
    def _set_frequency(self, freq: int) -> bool:
        """Set RTL-SDR frequency via rtl_tcp command"""
        try:
            cmd = struct.pack("<BI", 0x01, freq)  # 0x01 = SET_FREQUENCY
            self.socket.sendall(cmd)
            return True
        except Exception as e:
            logger.error(f"Failed to set frequency: {e}")
            return False
    
    def _set_sample_rate(self, rate: int) -> bool:
        """Set RTL-SDR sample rate via rtl_tcp command"""
        try:
            cmd = struct.pack("<BI", 0x02, rate)  # 0x02 = SET_SAMPLE_RATE
            self.socket.sendall(cmd)
            return True
        except Exception as e:
            logger.error(f"Failed to set sample rate: {e}")
            return False
    
    def _set_gain(self, gain: int) -> bool:
        """Set RTL-SDR gain via rtl_tcp command"""
        try:
            cmd = struct.pack("<BI", 0x04, gain)  # 0x04 = SET_GAIN
            self.socket.sendall(cmd)
            return True
        except Exception as e:
            logger.error(f"Failed to set gain: {e}")
            return False
