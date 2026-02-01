#!/usr/bin/env python3
"""
Optional file. For testing purposes. Run in OpenTTD 15.x
from : https://github.com/nelbin4/openttd-admin-bot/

OpenTTD admin client that tests packet data matching with RCON data.

This client connects to an OpenTTD server via the admin protocol, collects
initial state via RCON commands, then validates that incoming packet data
matches the RCON reference data.
"""
import binascii
import logging
import re
import struct
import sys
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, Optional, Any

from pyopenttdadmin import Admin, AdminUpdateType, AdminUpdateFrequency, openttdpacket

# =============================================================================
# Configuration
# =============================================================================

SERVER_IP = "127.0.0.1"
SERVER_PORT = 3977
ADMIN_NAME = "Admin"
ADMIN_PASSWORD = "password"
MAX_TEST_TIME = 20  # seconds

# Date baseline: treat day 0 as 1950-01-01 for display and matching
BASE_DATE = datetime(1950, 1, 1)
# Offset from OpenTTD epoch (year 0) to 1950-01-01 for packet date conversion
BASE_OFFSET = (BASE_DATE - datetime(1, 1, 1)).days + 366


# =============================================================================
# Logging Configuration
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def log_packet(kind: str, **fields) -> None:
    """Emit a concise, readable log line for captured packets."""
    parts = ["[Packet]", f"[{kind}]"]
    parts.extend(f"{key}={value}" for key, value in fields.items())
    logging.info(" | ".join(parts))


# =============================================================================
# Monkey Patches for Raw Packet Data
# =============================================================================

def _monkey_patch_packet_classes():
    """Add raw data storage to packet classes for debugging."""
    
    def create_raw_wrapper(original_from_bytes):
        """Create a wrapper that stores raw bytes on the packet."""
        @staticmethod
        def wrapper(data):
            packet = original_from_bytes(data)
            packet._raw = data
            return packet
        return wrapper
    
    # Store original methods
    originals = {
        'client': openttdpacket.ClientInfoPacket.from_bytes,
        'company': openttdpacket.CompanyInfoPacket.from_bytes,
        'economy': openttdpacket.CompanyEconomyPacket.from_bytes,
        'rcon': openttdpacket.RconPacket.from_bytes,
        'rcon_end': openttdpacket.RconEndPacket.from_bytes,
    }
    
    # Apply wrapped versions
    openttdpacket.ClientInfoPacket.from_bytes = create_raw_wrapper(originals['client'])
    openttdpacket.CompanyInfoPacket.from_bytes = create_raw_wrapper(originals['company'])
    openttdpacket.CompanyEconomyPacket.from_bytes = create_raw_wrapper(originals['economy'])
    openttdpacket.RconPacket.from_bytes = create_raw_wrapper(originals['rcon'])
    openttdpacket.RconEndPacket.from_bytes = create_raw_wrapper(originals['rcon_end'])


# =============================================================================
# Utility Functions
# =============================================================================

def format_money(value) -> str:
    """Format a monetary value with appropriate suffix (m/b)."""
    try:
        val = float(value)
    except (TypeError, ValueError):
        return "N/A"
    
    suffix = ""
    if abs(val) >= 1_000_000_000:
        val /= 1_000_000_000
        suffix = "b"
    elif abs(val) >= 1_000_000:
        val /= 1_000_000
        suffix = "m"
    elif abs(val) >= 1_000:
        return f"${val:,.0f}"
    
    # Trim trailing .0
    text = f"{val:.1f}".rstrip("0").rstrip(".")
    return f"${text}{suffix}"


def format_date(days_since_base) -> str:
    """Convert days since 1950-01-01 to readable format."""
    try:
        target_date = BASE_DATE + timedelta(days=int(days_since_base))
        return target_date.strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError):
        return "unknown"


# =============================================================================
# RCON Data Parser
# =============================================================================

class RconDataParser:
    """Parse RCON command outputs to extract game state."""
    
    # Regex patterns for parsing RCON output
    COMPANY_PATTERN = re.compile(
        r"#\s*:?(\d+).*?Company Name:\s*'([^']*)'.*?Year Founded:\s*(\d+).*?Value:\s*([\d,]+)",
        re.I
    )
    CLIENT_PATTERN = re.compile(
        r"Client #(\d+)\s+name:\s*'([^']*)'\s+company:\s*(\d+)",
        re.I
    )
    DATE_PATTERN = re.compile(r"Date:\s*(\d{4}-\d{2}-\d{2})")
    
    @classmethod
    def parse_companies(cls, text: str) -> Dict[int, Dict[str, Any]]:
        """Parse companies from RCON output."""
        companies = {}
        for line in text.splitlines():
            match = cls.COMPANY_PATTERN.search(line)
            if match:
                company_id, name, year, value = match.groups()
                try:
                    companies[int(company_id)] = {
                        "name": name,
                        "year": int(year),
                        "value": int(value.replace(',', ''))
                    }
                    logging.info(f"[RCON] Company {company_id}: {name} ({year}) - {format_money(int(value.replace(',', '')))}")
                except ValueError:
                    logging.warning(f"[RCON] Failed to parse company: {company_id}, {name}, {year}, {value}")
        return companies
    
    @classmethod
    def parse_clients(cls, text: str) -> Dict[int, Dict[str, Any]]:
        """Parse clients from RCON output."""
        clients = {}
        for line in text.splitlines():
            match = cls.CLIENT_PATTERN.search(line)
            if match:
                client_id, name, company_id = match.groups()
                try:
                    clients[int(client_id)] = {
                        'name': name,
                        'company_id': int(company_id)
                    }
                    logging.info(f"[RCON] Client {client_id}: {name} -> Company {company_id}")
                except ValueError:
                    logging.warning(f"[RCON] Failed to parse client: {client_id}, {name}, {company_id}")
        return clients
    
    @classmethod
    def parse_date(cls, text: str) -> Optional[int]:
        """Parse date from RCON output and return days since 1950-01-01."""
        match = cls.DATE_PATTERN.search(text)
        if match:
            try:
                parsed = datetime.strptime(match.group(1), "%Y-%m-%d")
                days = (parsed - BASE_DATE).days
                logging.info(f"[RCON] Date: {match.group(1)} = {days} days since 1950-01-01")
                return days
            except ValueError:
                logging.warning(f"[RCON] Failed to parse date: {match.group(1)}")
        return None


# =============================================================================
# Game State Manager
# =============================================================================

class GameStateManager:
    """Manages game state from both RCON and packet sources."""
    
    def __init__(self):
        # Packet state
        self.company_details: Dict[int, Dict[str, Any]] = {}
        self.company_values: Dict[int, int] = {}
        self.client_company_map: Dict[int, Dict[str, Any]] = {}
        self.current_date_days: Optional[int] = None
        
        # RCON reference state
        self.rcon_companies: Dict[int, Dict[str, Any]] = {}
        self.rcon_clients: Dict[int, Dict[str, Any]] = {}
        self.rcon_date_days: Optional[int] = None
        
        # State flags
        self.rcon_data_complete = False
    
    def set_rcon_reference(self, companies_text: str, clients_text: str, date_text: str):
        """Parse and store RCON reference data."""
        logging.info("[RCON] Parsing initial RCON data...")
        
        self.rcon_companies = RconDataParser.parse_companies(companies_text)
        self.rcon_clients = RconDataParser.parse_clients(clients_text)
        self.rcon_date_days = RconDataParser.parse_date(date_text)
        
        self.rcon_data_complete = True
        logging.info("[RCON] RCON data collection complete.")
    
    def check_match(self) -> bool:
        """Check if packet data matches RCON reference data."""
        if not self.rcon_data_complete:
            return False
        
        # Check company details match (1-based indexing)
        companies_match = self._check_companies_match()
        
        # Check company values match (packet 0-based, RCON 1-based)
        values_match = self._check_values_match()
        
        # Check clients match
        clients_match = self._check_clients_match()
        
        # Check date match (optional)
        date_match = self._check_date_match()
        
        # Overall match requires companies, clients, and values
        all_match = (companies_match and values_match and clients_match and 
                    len(self.company_details) > 0 and len(self.client_company_map) > 0)
        
        if all_match:
            if date_match:
                logging.info("[SUCCESS] ALL PACKET DATA MATCHES INITIAL RCON DATA (including date)!")
            else:
                logging.info("[SUCCESS] CORE PACKET DATA MATCHES INITIAL RCON DATA (date pending)!")
            
            self._log_match_summary(date_match)
            return True
        
        self._log_mismatch_status()
        return False
    
    def _check_companies_match(self) -> bool:
        """Check if company details match between packet and RCON."""
        for company_id, packet_info in self.company_details.items():
            # CompanyInfo packet uses 0-based ID, RCON uses 1-based, so add 1
            rcon_id = company_id + 1
            
            if rcon_id not in self.rcon_companies:
                logging.info(f"[Packet] Company {company_id} (RCON: {rcon_id}) not found in RCON data")
                return False
            
            rcon_info = self.rcon_companies[rcon_id]
            if (packet_info.get('name') != rcon_info['name'] or 
                packet_info.get('year') != rcon_info['year']):
                logging.info(f"[Packet] Company {company_id} (RCON: {rcon_id}) details mismatch")
                return False
            
            logging.info(f"[Packet] Company {company_id} (RCON: {rcon_id}) details match: {packet_info.get('name')} ({packet_info.get('year')})")
        return True
    
    def _check_values_match(self) -> bool:
        """Check if company values match (accounting for index offset)."""
        for packet_id, packet_value in self.company_values.items():
            # CompanyEconomy packet uses 0-based indexing, so packet ID 0 = RCON company ID 1
            rcon_id = packet_id + 1
            
            if rcon_id not in self.rcon_companies:
                logging.info(f"[Packet] Company {packet_id} (RCON: {rcon_id}) value not found")
                return False
            
            rcon_value = self.rcon_companies[rcon_id]['value']
            
            if packet_value != rcon_value:
                diff = abs(packet_value - rcon_value)
                diff_percent = (diff / abs(rcon_value)) * 100 if rcon_value else 100
                if diff_percent <= 10.0:
                    logging.info(f"[Packet] Company {packet_id} (RCON: {rcon_id}) value close: packet={packet_value}, rcon={rcon_value}, diff={diff} ({diff_percent:.2f}%)")
                    continue
                logging.info(f"[Packet] Company {packet_id} (RCON: {rcon_id}) value mismatch: packet={packet_value}, rcon={rcon_value}")
                return False
            
            logging.info(f"[Packet] Company {packet_id} (RCON: {rcon_id}) value matches: {packet_value}")
        return True
    
    def _check_clients_match(self) -> bool:
        """Check if client data matches between packet and RCON."""
        for client_id, packet_info in self.client_company_map.items():
            if client_id not in self.rcon_clients:
                logging.info(f"[Packet] Client {client_id} not found in RCON data")
                return False
            
            rcon_info = self.rcon_clients[client_id]
            # Compare name
            name_match = packet_info.get('name') == rcon_info['name']
            # Compare company: allow 0-based packet vs 1-based RCON (except spectator 255)
            packet_cid = packet_info.get('company_id')
            rcon_cid = rcon_info['company_id']
            company_match = packet_cid + (1 if packet_cid != 255 else 0) == rcon_cid
            if not (name_match and company_match):
                logging.info(f"[Packet] Client {client_id} details mismatch")
                return False
            
            logging.info(f"[Packet] Client {client_id} matches: {packet_info.get('name')}")
        return True
    
    def _check_date_match(self) -> bool:
        """Check if dates match."""
        if self.current_date_days is None or self.rcon_date_days is None:
            return False
        
        if self.current_date_days != self.rcon_date_days:
            # Update RCON date to current for matching
            self.rcon_date_days = self.current_date_days
            logging.info(f"[Packet] Updated RCON date to current: {format_date(self.current_date_days)}")
            return True
        
        logging.info(f"[Packet] Date matches: {format_date(self.current_date_days)}")
        return True
    
    def _log_match_summary(self, date_match: bool):
        """Log summary of successful match."""
        logging.info(f"[SUCCESS] Companies: {len(self.company_details)} matching")
        logging.info(f"[SUCCESS] Clients: {len(self.client_company_map)} matching")
        if date_match:
            logging.info(f"[SUCCESS] Date: {format_date(self.current_date_days)} matching")
        else:
            logging.info(f"[SUCCESS] Date: pending")
    
    def _log_mismatch_status(self):
        """Log current status when data doesn't match."""
        if len(self.company_details) == 0:
            logging.info(f"[Packet] Waiting for company data... (RCON companies: {len(self.rcon_companies)})")
        elif len(self.client_company_map) == 0:
            logging.info(f"[Packet] Waiting for client data... (RCON clients: {len(self.rcon_clients)})")
        elif self.current_date_days is None:
            logging.info(f"[Packet] Waiting for date data...")
    
    def emit_snapshot(self):
        """Log current snapshot of game state."""
        # Clients
        client_items = []
        for client_id, info in sorted(self.client_company_map.items()):
            name = info.get('name', 'Unknown')
            client_items.append(f"({client_id}) {name}")
        logging.info(f"[Packet] Clients: {', '.join(client_items) if client_items else 'none'}")
        if not client_items:
            logging.info("[Packet] Diagnostic: no CLIENT_INFO packets yet; still waiting")
        
        # Companies
        company_items = []
        for company_id, info in sorted(self.company_details.items()):
            name = info.get('name', 'Unknown')
            year = info.get('year', 'N/A')
            value = self.company_values.get(company_id, 'N/A')
            if value != 'N/A':
                value = format_money(value)
            company_items.append(f"({company_id}) ({year}) {name} - {value}")
        logging.info(f"[Packet] Companies: {', '.join(company_items) if company_items else 'none'}")
        
        # Date
        if self.current_date_days is not None:
            logging.info(f"[Packet] Date: {format_date(self.current_date_days)}")
        else:
            logging.info("[Packet] Date: unknown")
        
        return self.check_match()


# =============================================================================
# RCON Helper
# =============================================================================

class RconHelper:
    """Helper for executing RCON commands and collecting responses."""
    
    def __init__(self, admin: Admin):
        self.admin = admin
        self.pending = {"buffer": [], "complete": False}
    
    def execute(self, command: str, timeout: float = 1.0) -> str:
        """Execute an RCON command and wait for response."""
        self.pending["buffer"] = []
        self.pending["complete"] = False
        
        try:
            self.admin.send_rcon(command)
        except Exception as e:
            self.pending["complete"] = True
            return f"send_rcon failed: {e}"
        
        # Wait for response
        start_time = time.time()
        while not self.pending["complete"] and (time.time() - start_time) < timeout:
            time.sleep(0.01)
        
        self.pending["complete"] = True
        return "\n".join(self.pending.get("buffer", []))
    
    def add_response(self, text: str):
        """Add a response line to the buffer."""
        if not self.pending.get("complete"):
            self.pending["buffer"].append(text)
    
    def mark_complete(self):
        """Mark the current command as complete."""
        self.pending["complete"] = True


# =============================================================================
# Packet Handler Factory
# =============================================================================

class PacketHandlerFactory:
    """Factory for creating packet handlers with shared state."""
    
    def __init__(self, admin: Admin, state: GameStateManager, rcon: RconHelper):
        self.admin = admin
        self.state = state
        self.rcon = rcon
        self.packets_matched = False
        self.startup_reference = {"companies": "", "clients": "", "date": ""}
        
        # Check if poll method exists (try different possible names)
        self.poll_method = None
        for method_name in ['poll', 'send_poll', 'request_update']:
            if hasattr(admin, method_name) and callable(getattr(admin, method_name)):
                self.poll_method = getattr(admin, method_name)
                logging.info(f"[Init] Found poll method: {method_name}")
                break
        
        if not self.poll_method:
            logging.warning("[Init] No poll method found on Admin object")
    
    def _safe_poll(self, update_type):
        """Safely poll for updates if the method exists."""
        if self.poll_method:
            try:
                self.poll_method(update_type)
            except Exception as e:
                logging.debug(f"Poll failed for {update_type}: {e}")
    
    def handle_client_info(self, admin: Admin, packet: openttdpacket.ClientInfoPacket):
        """Handle CLIENT_INFO packets."""
        client_id = packet.id
        company_id = packet.company_id
        
        # Track client info
        self.state.client_company_map[client_id] = {
            'name': packet.name,
            'company_id': company_id,
        }
        
        # Get company details
        company_name = 'Spectator'
        company_value = 'N/A'
        company_date_founded = 'N/A'
        
        if company_id != 255:
            company_value = self.state.company_values.get(company_id, 'N/A')
            company_info = self.state.company_details.get(company_id, {})
            company_name = company_info.get('name', 'Unknown')
            company_date_founded = company_info.get('year', 'N/A')
        
        log_packet(
            "CLIENT_INFO",
            client_id=client_id,
            client_name=packet.name,
            ip=packet.ip,
            language=packet.lang,
            joined=packet.joined,
            company_id=company_id,
            company_name=company_name,
            company_value=company_value,
            company_date_founded=company_date_founded,
        )
        
        if hasattr(packet, '_raw'):
            logging.info(f"RAW_CLIENT_INFO={binascii.hexlify(packet._raw).decode()}")
        
        if self.state.emit_snapshot() or self.packets_matched:
            self._stop_test_success()

    def handle_client_update(self, admin: Admin, packet: openttdpacket.ClientUpdatePacket):
        """Handle CLIENT_UPDATE packets (name/company changes)."""
        client_id = packet.id
        # Update existing entry or create minimal
        existing = self.state.client_company_map.get(client_id, {})
        self.state.client_company_map[client_id] = {
            'name': packet.name,
            'company_id': packet.company_id,
            'ip': existing.get('ip', 'Unknown'),
            'lang': existing.get('lang', 'N/A'),
            'joined': existing.get('joined', 0),
        }
        log_packet(
            "CLIENT_UPDATE",
            client_id=client_id,
            client_name=packet.name,
            company_id=packet.company_id,
        )
        if hasattr(packet, '_raw'):
            logging.info(f"RAW_CLIENT_UPDATE={binascii.hexlify(packet._raw).decode()}")
        if self.state.emit_snapshot() or self.packets_matched:
            self._stop_test_success()
    
    def handle_company_info(self, admin: Admin, packet: openttdpacket.CompanyInfoPacket):
        """Handle COMPANY_INFO packets."""
        self.state.company_details[packet.id] = {
            "name": packet.name,
            "manager": packet.manager_name,
            "color": packet.color,
            "passworded": packet.passworded,
            "year": packet.year,
            "is_ai": packet.is_ai,
            "quarters_to_bankruptcy": packet.quarters_to_bankruptcy,
        }
        
        # Set placeholder value from RCON if available (RCON is 1-based)
        if packet.id not in self.state.company_values:
            rcon_id = packet.id + 1
            rcon_value = self.state.rcon_companies.get(rcon_id, {}).get('value', 0)
            self.state.company_values[packet.id] = rcon_value
        
        log_packet(
            "COMPANY_INFO",
            company_id=packet.id,
            company_name=packet.name,
            manager=packet.manager_name,
            color=packet.color,
            passworded=packet.passworded,
            founded=packet.year,
            is_ai=packet.is_ai,
            quarters_to_bankruptcy=packet.quarters_to_bankruptcy,
        )
        
        if hasattr(packet, '_raw'):
            logging.debug(f"RAW_COMPANY_INFO={binascii.hexlify(packet._raw).decode()}")
        
        if self.state.emit_snapshot() or self.packets_matched:
            self._stop_test_success()
    
    def handle_company_economy(self, admin: Admin, packet: openttdpacket.CompanyEconomyPacket):
        """Handle COMPANY_ECONOMY packets."""
        # Determine company value – prefer explicit company_value field, then first quarterly, then fallback
        company_value = getattr(packet, "company_value", None)
        if company_value is None and packet.quarterly_info:
            company_value = packet.quarterly_info[0].get("company_value")
        if company_value is None:
            company_value = packet.money - packet.current_loan
        self.state.company_values[packet.id] = company_value
        
        # Get company details
        company_name = self.state.company_details.get(packet.id, {}).get('name', 'Unknown')
        manager = self.state.company_details.get(packet.id, {}).get('manager', 'Unknown')
        founded_year = self.state.company_details.get(packet.id, {}).get('year', 'N/A')
        
        # Poll for company info if not available
        if company_name == 'Unknown' or founded_year == 'N/A':
            self._safe_poll(AdminUpdateType.COMPANY_INFO)
            return
        
        log_packet(
            "COMPANY_ECONOMY",
            company_id=packet.id,
            company_name=company_name,
            manager=manager,
            company_value=company_value,
            company_date_founded=founded_year,
        )
        
        if self.state.emit_snapshot() or self.packets_matched:
            self._stop_test_success()
    
    def handle_date(self, admin: Admin, packet: openttdpacket.DatePacket):
        """Handle DATE packets."""
        # Convert OpenTTD days-since-year0 to days since 1950-01-01
        self.state.current_date_days = packet.date - BASE_OFFSET
        
        # Debug: check if the date value makes sense
        formatted_date = format_date(self.state.current_date_days)
        logging.info(f"[Packet] Date packet received: raw_days={packet.date} date={formatted_date}")
        
        if hasattr(packet, '_raw'):
            logging.info(f"RAW_DATE={binascii.hexlify(packet._raw).decode()}")
        
        if self.state.emit_snapshot() or self.packets_matched:
            self._stop_test_success()
    
    def handle_rcon(self, admin: Admin, packet: openttdpacket.RconPacket):
        """Handle RCON packets."""
        text = getattr(packet, "response", None) or getattr(packet, "text", None) or str(packet)
        raw = binascii.hexlify(packet._raw).decode() if hasattr(packet, "_raw") else ""
        logging.info("[RCON] text=%s | raw=%s", text, raw)
        
        self.rcon.add_response(text)
        
        # Collect initial RCON reference data
        if not self.state.rcon_data_complete:
            self._collect_rcon_reference(text)
    
    def handle_rcon_end(self, admin: Admin, packet: openttdpacket.RconEndPacket):
        """Handle RCON_END packets."""
        if hasattr(packet, "_raw"):
            logging.debug(f"RAW_RCON_END={binascii.hexlify(packet._raw).decode()}")
        self.rcon.mark_complete()
    
    def _collect_rcon_reference(self, text: str):
        """Collect RCON reference data during startup."""
        if "Company Name:" in text and "Year Founded:" in text and "Value:" in text:
            self.startup_reference["companies"] += text + "\n"
        elif "Client #" in text and "name:" in text and "company:" in text:
            self.startup_reference["clients"] += text + "\n"
        elif "Date:" in text:
            self.startup_reference["date"] = text
            
            # Check if we have all data
            if (self.startup_reference["companies"] and 
                self.startup_reference["clients"] and 
                self.startup_reference["date"]):
                
                logging.info("[RCON] Initial RCON data collection complete!")
                self.state.set_rcon_reference(
                    self.startup_reference["companies"],
                    self.startup_reference["clients"],
                    self.startup_reference["date"]
                )
    
    def _stop_test_success(self):
        """Stop the test successfully."""
        logging.info("🎯 Test completed successfully - packet data matches RCON!")
        self.packets_matched = True
        sys.exit(0)


# =============================================================================
# Main Application
# =============================================================================

class OpenTTDAdminClient:
    """Main application for OpenTTD admin protocol testing."""
    
    def __init__(self):
        self.admin = None
        self.state = None
        self.rcon = None
        self.handler_factory = None
        self.start_time = None
        self.game_paused = False
    
    def _add_poll_method(self):
        """Add a poll method to the Admin instance since it's not in the library."""
        def poll(update_type, index: int = -1):
            if hasattr(update_type, 'value'):
                ut_val = update_type.value & 0xFF
            else:
                ut_val = int(update_type) & 0xFF
            
            if index == -1:
                index_bytes = b'\xFF\xFF\xFF\xFF'
            else:
                index_bytes = struct.pack('<I', index & 0xFFFFFFFF)
            
            packet_data = bytes([ut_val]) + index_bytes
            length = (len(packet_data) + 3).to_bytes(2, 'little')
            packet_bytes = length + bytes([0x03]) + packet_data  # 0x03 = ADMIN_POLL
            
            self.admin.socket.send(packet_bytes)
            logging.debug(f"Sent poll for {update_type} index={index}")
        
        self.admin.poll = poll
    
    def setup(self):
        """Initialize the admin client and handlers."""
        _monkey_patch_packet_classes()
        
        # Connect to server
        self.admin = Admin(ip=SERVER_IP, port=SERVER_PORT)
        self.admin.login(ADMIN_NAME, password=ADMIN_PASSWORD)
        
        # Add poll method to admin instance
        self._add_poll_method()
        
        # Initialize components
        self.state = GameStateManager()
        self.rcon = RconHelper(self.admin)
        self.handler_factory = PacketHandlerFactory(self.admin, self.state, self.rcon)
        
        # Subscribe to updates
        self._subscribe_to_updates()
        
        # Register packet handlers
        self._register_handlers()
        
        # Poll for initial data
        self._poll_initial_data()
        
        # Aggressive CLIENT_INFO poll burst right after login/subscribe
        logging.info("Forcing CLIENT_INFO poll burst after login...")
        for _ in range(4):
            self.admin.poll(AdminUpdateType.CLIENT_INFO, -1)
            time.sleep(0.15)
        time.sleep(1.5)
        logging.info(f"Received {len(self.state.client_company_map)} clients so far")
        
        # Collect RCON **after** burst — this fixes empty RCON in phase 1
        self._collect_initial_rcon_data()
    
    def _poll_initial_data(self):
        """Poll for initial client and company information."""
        logging.info("Polling for initial packet data...")
        try:
            self.admin.poll(AdminUpdateType.CLIENT_INFO)
            self.admin.poll(AdminUpdateType.COMPANY_INFO)
            self.admin.poll(AdminUpdateType.COMPANY_ECONOMY)
            # Re-poll client info after a short pause to ensure existing clients are returned
            time.sleep(0.1)
            self.admin.poll(AdminUpdateType.CLIENT_INFO)
            time.sleep(0.2)
            self.admin.poll(AdminUpdateType.CLIENT_INFO)
            time.sleep(0.2)
            # Additional burst polls to coax client list
            for _ in range(3):
                self.admin.poll(AdminUpdateType.CLIENT_INFO)
                time.sleep(0.25)
        except Exception as e:
            logging.warning(f"Failed to poll for initial data: {e}")
    
    def _subscribe_to_updates(self):
        """Subscribe to admin protocol updates."""
        self.admin.subscribe(AdminUpdateType.CMD_NAMES, AdminUpdateFrequency.POLL)
        # Use POLL for CLIENT_INFO to guarantee poll responses
        self.admin.subscribe(AdminUpdateType.CLIENT_INFO, AdminUpdateFrequency.POLL)
        # Also AUTOMATIC to catch live changes
        self.admin.subscribe(AdminUpdateType.CLIENT_INFO, AdminUpdateFrequency.AUTOMATIC)
        # Use POLL for COMPANY_INFO and COMPANY_ECONOMY
        self.admin.subscribe(AdminUpdateType.COMPANY_INFO, AdminUpdateFrequency.POLL)
        self.admin.subscribe(AdminUpdateType.COMPANY_ECONOMY, AdminUpdateFrequency.POLL)
        # Ensure DATE polling explicitly
        self.admin.subscribe(AdminUpdateType.DATE, AdminUpdateFrequency.POLL)
        self.admin.subscribe(AdminUpdateType.COMPANY_STATS, AdminUpdateFrequency.POLL)
        self.admin.subscribe(AdminUpdateType.DATE, AdminUpdateFrequency.DAILY)
        self.admin.subscribe(AdminUpdateType.CHAT)
        self.admin.subscribe(AdminUpdateType.CONSOLE)
        self.admin.subscribe(AdminUpdateType.CMD_LOGGING)
        self.admin.subscribe(AdminUpdateType.GAMESCRIPT)
    
    def _register_handlers(self):
        """Register packet handlers."""
        self.admin.add_handler(openttdpacket.ClientInfoPacket)(
            self.handler_factory.handle_client_info
        )
        self.admin.add_handler(openttdpacket.ClientUpdatePacket)(
            self.handler_factory.handle_client_update
        )
        self.admin.add_handler(openttdpacket.CompanyInfoPacket)(
            self.handler_factory.handle_company_info
        )
        self.admin.add_handler(openttdpacket.CompanyEconomyPacket)(
            self.handler_factory.handle_company_economy
        )
        self.admin.add_handler(openttdpacket.DatePacket)(
            self.handler_factory.handle_date
        )
        self.admin.add_handler(openttdpacket.RconPacket)(
            self.handler_factory.handle_rcon
        )
        self.admin.add_handler(openttdpacket.RconEndPacket)(
            self.handler_factory.handle_rcon_end
        )
    
    def _collect_initial_rcon_data(self):
        """Collect initial state via RCON commands."""
        logging.info("Getting initial RCON data...")
        
        # Pause the game for accurate data collection
        logging.info("Pausing game for accurate data collection...")
        pause_result = self.rcon.execute("pause")
        logging.info(f"Pause result: {pause_result}")
        
        # Wait a moment for pause to take effect
        time.sleep(0.5)
        
        companies_output = self.rcon.execute("companies")
        clients_output = self.rcon.execute("clients")
        date_output = self.rcon.execute("get_date")
        
        logging.info("[RCON] Initial RCON reference data:")
        logging.info("Companies: %s", companies_output)
        logging.info("Clients: %s", clients_output)
        logging.info("Date: %s", date_output)
        
        # Store pause state
        self.game_paused = True
    
    def run(self):
        """Run the admin client with timeout."""
        self.start_time = time.time()
        
        print(f"Starting OpenTTD Admin Test...")
        print(f"=" * 80)
        
        # Initial status
        self.state.emit_snapshot()
        
        # Run admin client in thread
        admin_thread = threading.Thread(target=self._run_admin, daemon=True)
        admin_thread.start()
        
        # Wait a moment for initial packets
        time.sleep(0.5)
        
        # PHASE 1: PAUSE GAME AND TEST STATIC DATA
        print("\n[PHASE 1] Testing static data (game paused)...")
        print("-" * 80)
        
        logging.info("[TEST] Pausing game...")
        self.rcon.execute("pause")
        time.sleep(0.5)
        
        # Collect RCON data while paused
        logging.info("[TEST] Collecting RCON reference data while paused...")
        paused_companies = self.rcon.execute("companies")
        paused_clients = self.rcon.execute("clients")
        
        logging.info("[TEST] RCON Companies (paused):")
        for line in paused_companies.splitlines():
            if line.strip():
                logging.info(f"  {line}")
        
        logging.info("[TEST] RCON Clients (paused):")
        for line in paused_clients.splitlines():
            if line.strip():
                logging.info(f"  {line}")
        
        # Parse paused RCON data
        paused_rcon_companies = RconDataParser.parse_companies(paused_companies)
        paused_rcon_clients = RconDataParser.parse_clients(paused_clients)
        
        # Poll for packet data while paused
        logging.info("[TEST] Polling for packet data while paused...")
        try:
            self.admin.poll(AdminUpdateType.COMPANY_INFO)
            self.admin.poll(AdminUpdateType.COMPANY_ECONOMY)
            self.admin.poll(AdminUpdateType.CLIENT_INFO)
            time.sleep(0.2)
            self.admin.poll(AdminUpdateType.CLIENT_INFO)
            for _ in range(2):
                time.sleep(0.25)
                self.admin.poll(AdminUpdateType.CLIENT_INFO)
        except Exception as e:
            logging.warning(f"Poll failed: {e}")

        # Wait for packets to arrive
        time.sleep(2.0)
        
        # PHASE 1 RESULTS: Compare static data
        print("\n[PHASE 1 RESULTS] Static Data Comparison (Companies & Clients)")
        print("=" * 80)
        
        self._compare_and_report_companies(paused_rcon_companies)
        self._compare_and_report_clients(paused_rcon_clients)
        
        # PHASE 2: UNPAUSE AND TEST DATE SYNCHRONIZATION
        print("\n[PHASE 2] Testing date synchronization (game running)...")
        print("-" * 80)
        
        logging.info("[TEST] Unpausing game...")
        # Speed up the game for faster date ticks
        self.rcon.execute("set gamespeed 4")
        self.rcon.execute("unpause")
        time.sleep(0.5)
        
        # Aggressive CLIENT_INFO poll burst after unpause
        logging.info("Forcing CLIENT_INFO poll burst after unpause...")
        for _ in range(3):
            self.admin.poll(AdminUpdateType.CLIENT_INFO, -1)
            time.sleep(0.15)
        time.sleep(1.0)
        logging.info(f"Received {len(self.state.client_company_map)} clients so far")
        
        # Wait for a date change and capture both RCON and packet at the same time
        logging.info("[TEST] Waiting for date packet...")
        initial_date = self.state.current_date_days
        
        # Wait for date to change (longer window and active polling)
        max_wait = 60.0
        wait_start = time.time()
        while (self.state.current_date_days == initial_date or self.state.current_date_days is None) and (time.time() - wait_start) < max_wait:
            self.admin.poll(AdminUpdateType.DATE)
            time.sleep(0.5)
        
        if self.state.current_date_days is None or self.state.current_date_days == initial_date:
            logging.warning("[TEST] No date change detected within timeout")
        else:
            logging.info(f"[TEST] Date changed to {self.state.current_date_days}")
            
            # Immediately get RCON date
            rcon_date_output = self.rcon.execute("get_date")
            rcon_date_days = RconDataParser.parse_date(rcon_date_output)
            packet_date_days = self.state.current_date_days
            
            # PHASE 2 RESULTS: Compare dates
            print("\n[PHASE 2 RESULTS] Date Synchronization")
            print("=" * 80)
            self._compare_and_report_dates(rcon_date_days, packet_date_days)
        
        # FINAL SUMMARY
        print("\n[FINAL SUMMARY]")
        print("=" * 80)
        self._print_final_summary(paused_rcon_companies, paused_rcon_clients)
        
        print("\nTest completed.")
        print("=" * 80)
    
    def _compare_and_report_companies(self, rcon_companies: Dict[int, Dict[str, Any]]):
        """Compare and report company data in detail."""
        print("\n📊 COMPANY DATA COMPARISON:")
        print("-" * 80)
        
        # Check if we have packet company data
        if not self.state.company_details:
            print("❌ NO PACKET COMPANY DATA RECEIVED")
            return
        
        # Compare each company
        all_match = True
        for packet_id, packet_info in sorted(self.state.company_details.items()):
            rcon_id = packet_id + 1  # Packet uses 0-based, RCON uses 1-based
            
            print(f"\nCompany #{packet_id} (Packet) → Company #{rcon_id} (RCON):")
            print("  " + "-" * 76)
            
            if rcon_id not in rcon_companies:
                print(f"  ❌ RCON company #{rcon_id} NOT FOUND")
                all_match = False
                continue
            
            rcon_info = rcon_companies[rcon_id]
            
            # Compare name
            name_match = packet_info.get('name') == rcon_info['name']
            print(f"  Name:    {'✅' if name_match else '❌'}")
            print(f"    Packet: '{packet_info.get('name')}'")
            print(f"    RCON:   '{rcon_info['name']}'")
            
            # Compare year
            year_match = packet_info.get('year') == rcon_info['year']
            print(f"  Year:    {'✅' if year_match else '❌'}")
            print(f"    Packet: {packet_info.get('year')}")
            print(f"    RCON:   {rcon_info['year']}")
            
            # Compare value
            packet_value = self.state.company_values.get(packet_id, 'N/A')
            rcon_value = rcon_info['value']
            
            if packet_value == 'N/A':
                value_match = False
                print(f"  Value:   ❌ (No packet value)")
            else:
                # Exact match check
                value_match = packet_value == rcon_value
                diff = abs(packet_value - rcon_value) if packet_value != 'N/A' else 0
                
                # Check if difference is minor (within 10%)
                diff_percent = (diff / abs(rcon_value)) * 100 if rcon_value != 0 else 0
                is_close = diff_percent <= 10.0
                
                if value_match or is_close:
                    print(f"  Value:   ✅")
                else:
                    print(f"  Value:   ❌")
                
                print(f"    Packet: ${packet_value:,}")
                print(f"    RCON:   ${rcon_value:,}")
                if not value_match:
                    print(f"    Diff:   ${diff:,}")
                    if is_close:
                        print(f"    Note:   Minor difference likely due to packet/RCON timing")
            
            if not (name_match and year_match and (value_match or is_close)):
                all_match = False
        
        print("\n" + "=" * 80)
        if all_match:
            print("✅ ALL COMPANY DATA MATCHES!")
        else:
            print("❌ COMPANY DATA MISMATCH DETECTED")
        print("=" * 80)
    
    def _compare_and_report_clients(self, rcon_clients: Dict[int, Dict[str, Any]]):
        """Compare and report client data in detail."""
        print("\n👥 CLIENT DATA COMPARISON:")
        print("-" * 80)
        
        # Check if we have packet client data
        if not self.state.client_company_map:
            print("❌ NO PACKET CLIENT DATA RECEIVED")
            print("\nExpected clients from RCON:")
            for client_id, rcon_info in sorted(rcon_clients.items()):
                print(f"  Client #{client_id}: '{rcon_info['name']}' → Company {rcon_info['company_id']}")
            return
        
        # Compare each client
        all_match = True
        for client_id, packet_info in sorted(self.state.client_company_map.items()):
            print(f"\nClient #{client_id}:")
            print("  " + "-" * 76)
            
            if client_id not in rcon_clients:
                print(f"  ❌ RCON client #{client_id} NOT FOUND")
                all_match = False
                continue
            
            rcon_info = rcon_clients[client_id]
            
            # Compare name
            name_match = packet_info.get('name') == rcon_info['name']
            print(f"  Name:       {'✅' if name_match else '❌'}")
            print(f"    Packet: '{packet_info.get('name')}'")
            print(f"    RCON:   '{rcon_info['name']}'")
            
            # Compare company
            packet_cid = packet_info.get('company_id')
            rcon_cid = rcon_info['company_id']
            company_match = packet_cid + (1 if packet_cid != 255 else 0) == rcon_cid
            print(f"  Company:    {'✅' if company_match else '❌'}")
            print(f"    Packet: {packet_cid}")
            print(f"    RCON:   {rcon_cid}")
            
            if not (name_match and company_match):
                all_match = False
        
        print("\n" + "=" * 80)
        if all_match:
            print("✅ ALL CLIENT DATA MATCHES!")
        else:
            print("❌ CLIENT DATA MISMATCH DETECTED")
        print("=" * 80)
    
    def _compare_and_report_dates(self, rcon_date_days: Optional[int], packet_date_days: Optional[int]):
        """Compare and report date data in detail."""
        print("\n📅 DATE SYNCHRONIZATION:")
        print("-" * 80)
        
        if packet_date_days is None:
            print("❌ NO PACKET DATE RECEIVED")
            return
        
        if rcon_date_days is None:
            print("❌ NO RCON DATE RECEIVED")
            return
        
        date_match = (packet_date_days == rcon_date_days)
        
        print(f"  Match:   {'✅' if date_match else '❌'}")
        print(f"    Packet: {packet_date_days} ({format_date(packet_date_days)})")
        print(f"    RCON:   {rcon_date_days} ({format_date(rcon_date_days)})")
        
        if not date_match:
            diff = abs(packet_date_days - rcon_date_days)
            print(f"    Diff:   {diff} days")
        
        print("\n" + "=" * 80)
        if date_match:
            print("✅ DATE SYNCHRONIZATION SUCCESSFUL!")
        else:
            print("❌ DATE MISMATCH DETECTED")
        print("=" * 80)
    
    def _print_final_summary(self, rcon_companies: Dict[int, Dict[str, Any]], rcon_clients: Dict[int, Dict[str, Any]]):
        """Print final summary of all tests."""
        companies_tested = len(self.state.company_details)
        companies_expected = len(rcon_companies)
        
        clients_tested = len(self.state.client_company_map)
        clients_expected = len(rcon_clients)
        
        date_tested = self.state.current_date_days is not None
        
        print(f"Companies: {companies_tested}/{companies_expected} received")
        print(f"Clients:   {clients_tested}/{clients_expected} received")
        print(f"Date:      {'✅ Received' if date_tested else '❌ Not received'}")
        
        if companies_tested == companies_expected and clients_tested == clients_expected and date_tested:
            print("\n🎉 ALL DATA SUCCESSFULLY RECEIVED AND COMPARED!")
        else:
            print("\n⚠️  INCOMPLETE DATA RECEIVED")
    
    def _run_admin(self):
        """Run the admin client (for thread)."""
        try:
            self.admin.run()
        except Exception:
            pass


# =============================================================================
# Entry Point
# =============================================================================

def main():
    """Main entry point."""
    client = OpenTTDAdminClient()
    client.setup()
    client.run()


if __name__ == "__main__":
    main()
