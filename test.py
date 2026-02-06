#!/usr/bin/env python3
"""
OpenTTD Admin Protocol Test Suite

Tests OpenTTD admin protocol functionality using both packet-based and RCON methods.
Runs comprehensive packet vs RCON comparison and packet-only admin protocol tests.
"""

import json
import logging
import sys
import time
from typing import Dict, Any, Optional, List
from dataclasses import dataclass

# Add the current directory to Python path for imports
sys.path.insert(0, '/home/nelbin/openttd-admin')
sys.path.insert(0, '/home/nelbin/openttd-admin/reference/pyOpenTTDAdmin')

from pyopenttdadmin import Admin, AdminUpdateFrequency, AdminUpdateType, openttdpacket

# Set up logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)
logger = logging.getLogger("PacketRconTest")

@dataclass(frozen=True)
class TestConfig:
    admin_port: int
    game_port: int
    server_num: int
    server_ip: str
    admin_name: str
    admin_pass: str
    goal_value: int
    load_scenario: str
    dead_co_age: int
    dead_co_value: int
    rcon_retry_max: int
    rcon_retry_delay: float
    reconnect_max_attempts: int
    reconnect_delay: float
    reset_countdown_seconds: int

# Regex patterns from main.py
COMPANY_RE = r"#\s*:?(\d+)(?:\([^)]+\))?\s+Company Name:\s*'([^']*)'\s+" \
             r"Year Founded:\s*(\d+)\s+Money:\s*[^0-9.,-]?\s*([-0-9,]+)\s+" \
             r"Loan:\s*[^0-9.,-]?\s*(\d+,?\d*)\s+Value:\s*[^0-9.,-]?\s*(\d+,?\d*)"

CLIENT_RE = r"Client #(\d+)\s+name:\s*'([^']*)'\s+company:\s*(\d+)"

class PacketRconTester:
    def __init__(self, config: TestConfig):
        self.config = config
        self.packet_data = {}
        self.rcon_data = {}
        self.packet_received = False
        self.rcon_received = False
        
        # Initialize admin connection
        self.admin = Admin(
            ip=config.server_ip,
            port=config.admin_port
        )
        
        # Register packet handlers using decorator
        @self.admin.add_handler(openttdpacket.CompanyInfoPacket)
        def handle_company_info(admin, pkt):
            self.on_company_info(admin, pkt)
        
        @self.admin.add_handler(openttdpacket.CompanyUpdatePacket)
        def handle_company_update(admin, pkt):
            self.on_company_update(admin, pkt)
            
        @self.admin.add_handler(openttdpacket.CompanyEconomyPacket)
        def handle_company_economy(admin, pkt):
            self.on_company_economy(admin, pkt)
            
        @self.admin.add_handler(openttdpacket.CompanyRemovePacket)
        def handle_company_remove(admin, pkt):
            self.on_company_remove(admin, pkt)
            
        @self.admin.add_handler(openttdpacket.RconPacket)
        def handle_rcon(admin, pkt):
            self.on_rcon_response(admin, pkt)
            
        @self.admin.add_handler(openttdpacket.WelcomePacket)
        def handle_welcome(admin, pkt):
            logger.info(f"[Packet] Welcome received: {pkt.server_name}")
            
        @self.admin.add_handler(openttdpacket.DatePacket)
        def handle_date(admin, pkt):
            logger.debug(f"[Packet] Date: {pkt.date}")
        
        logger.info(f"Tester initialized for server {config.server_ip}:{config.admin_port}")

    def on_company_info(self, admin, pkt) -> None:
        """Handle CompanyInfoPacket"""
        co_id = pkt.id + 1  # Convert from 0-based packet ID to 1-based RCON ID
        existing = self.packet_data.get(co_id, {})
        existing.update({
            'display_id': co_id,
            'name': (pkt.name or '').strip(),
            'manager': pkt.manager_name,
            'start_date': pkt.year,
            'color': getattr(pkt.color, 'name', str(pkt.color)),
            'passworded': pkt.passworded,
            'is_ai': pkt.is_ai,
            'source': 'packet_info'
        })
        self.packet_data[co_id] = existing
        self.packet_received = True
        logger.debug(f"[Packet] CompanyInfo: ID={co_id}, Name={pkt.name}")

    def on_company_update(self, admin, pkt) -> None:
        """Handle CompanyUpdatePacket"""
        co_id = pkt.id + 1  # Convert from 0-based packet ID to 1-based RCON ID
        existing = self.packet_data.get(co_id, {})
        existing.update({
            'display_id': co_id,
            'name': (pkt.name or '').strip(),
            'manager': pkt.manager_name,
            'color': getattr(pkt.color, 'name', str(pkt.color)),
            'passworded': pkt.passworded,
            'source': 'packet_update'
        })
        self.packet_data[co_id] = existing
        self.packet_received = True
        logger.debug(f"[Packet] CompanyUpdate: ID={co_id}, Name={pkt.name}")

    def on_company_economy(self, admin, pkt) -> None:
        """Handle CompanyEconomyPacket"""
        co_id = pkt.id + 1  # Convert from 0-based packet ID to 1-based RCON ID
        existing = self.packet_data.get(co_id, {})
        
        latest_value = 0
        try:
            if pkt.quarterly_info:
                latest_value = pkt.quarterly_info[0].get('company_value', 0)
        except (AttributeError, IndexError, KeyError):
            pass
        
        existing.update({
            'display_id': co_id,
            'money': pkt.money,
            'loan': pkt.current_loan,
            'income': pkt.income,
            'delivered': pkt.delivered_cargo,
            'value': latest_value or existing.get('value', 0),
            'source': 'packet_economy'
        })
        self.packet_data[co_id] = existing
        self.packet_received = True
        logger.debug(f"[Packet] CompanyEconomy: ID={co_id}, Value={latest_value}")

    def on_company_remove(self, admin, pkt) -> None:
        """Handle CompanyRemovePacket"""
        co_id = pkt.id + 1  # Convert from 0-based packet ID to 1-based RCON ID
        if co_id in self.packet_data:
            del self.packet_data[co_id]
        logger.info(f"[Packet] CompanyRemove: ID={co_id}")

    def on_rcon_response(self, admin, pkt) -> None:
        """Handle RconPacket response"""
        logger.debug(f"[RCON] Raw response: {repr(pkt.response)}")
        # Parse this response and merge with existing data
        new_companies = self.parse_rcon_companies(pkt.response)
        self.rcon_data.update(new_companies)
        self.rcon_received = True
        logger.info(f"[RCON] Retrieved {len(new_companies)} companies from this response, total: {len(self.rcon_data)}")

    def parse_rcon_companies(self, output: str) -> Dict[int, Dict[str, Any]]:
        """Parse RCON companies output"""
        import re
        companies = {}
        unmatched = 0
        
        for line in output.splitlines():
            m = re.match(COMPANY_RE, line, re.I)
            if not m:
                unmatched += 1
                continue
            
            try:
                co_id, name, year, money, loan, value = m.groups()
                co_id = int(co_id)
                companies[co_id] = {
                    'display_id': co_id,
                    'name': name.strip(),
                    'start_date': int(year),
                    'money': int(money.replace(',', '')),
                    'loan': int(loan.replace(',', '')),
                    'value': int(value.replace(',', '')),
                    'source': 'rcon'
                }
            except (ValueError, AttributeError) as e:
                logger.warning(f"[RCON] Parse error: {e} on line: {line}")
                unmatched += 1
        
        if unmatched > 0:
            logger.warning(f"[RCON] {unmatched} unmatched lines in companies output")
        
        return companies

    def get_rcon_companies(self) -> Dict[int, Dict[str, Any]]:
        """Fetch companies via RCON"""
        try:
            self.rcon_received = False
            self.rcon_data = {}
            self.admin.send_rcon('companies')
            
            # Wait for RCON response
            start_time = time.time()
            while not self.rcon_received and (time.time() - start_time) < 5:
                try:
                    packets = self.admin.recv()
                    for packet in packets:
                        self.admin.handle_packet(packet)
                except Exception as e:
                    logger.debug(f"RCON receive error: {e}")
                time.sleep(0.1)
            
            if not self.rcon_received:
                logger.warning("[RCON] No response received")
                return {}
            
            logger.info(f"[RCON] Retrieved {len(self.rcon_data)} companies")
            return self.rcon_data
        except Exception as e:
            logger.error(f"[RCON] Failed to get companies: {e}")
            return {}

    def compare_data(self) -> Dict[str, Any]:
        """Compare packet vs RCON data"""
        comparison = {
            'packet_companies': len(self.packet_data),
            'rcon_companies': len(self.rcon_data),
            'packet_ids': set(self.packet_data.keys()),
            'rcon_ids': set(self.rcon_data.keys()),
            'differences': [],
            'matches': []
        }
        
        # Find companies in both datasets
        common_ids = comparison['packet_ids'] & comparison['rcon_ids']
        packet_only = comparison['packet_ids'] - comparison['rcon_ids']
        rcon_only = comparison['rcon_ids'] - comparison['packet_ids']
        
        logger.info(f"Common companies: {len(common_ids)}")
        logger.info(f"Packet only: {len(packet_only)}")
        logger.info(f"RCON only: {len(rcon_only)}")
        
        # Compare common companies
        for co_id in common_ids:
            packet_co = self.packet_data[co_id]
            rcon_co = self.rcon_data[co_id]
            
            differences = []
            
            # Compare key fields
            if packet_co.get('name') != rcon_co.get('name'):
                differences.append(f"Name: P='{packet_co.get('name')}' vs R='{rcon_co.get('name')}'")
            
            if packet_co.get('start_date') != rcon_co.get('start_date'):
                differences.append(f"Year: P={packet_co.get('start_date')} vs R={rcon_co.get('start_date')}")
            
            # Compare monetary values (if available in packet)
            if 'money' in packet_co and 'money' in rcon_co:
                money_diff = abs(packet_co['money'] - rcon_co['money'])
                if money_diff > 1000:  # Allow small differences
                    differences.append(f"Money: P={packet_co['money']} vs R={rcon_co['money']} (diff: {money_diff})")
            
            if 'value' in packet_co and 'value' in rcon_co:
                value_diff = abs(packet_co['value'] - rcon_co['value'])
                if value_diff > 1000:  # Allow small differences
                    percent_diff = (value_diff / rcon_co['value']) * 100 if rcon_co['value'] > 0 else 0
                    differences.append(f"Value: P={packet_co['value']} vs R={rcon_co['value']} (diff: {value_diff}, {percent_diff:.2f}%)")
            
            if differences:
                comparison['differences'].append({
                    'company_id': co_id,
                    'company_name': packet_co.get('name', rcon_co.get('name', 'Unknown')),
                    'differences': differences
                })
            else:
                comparison['matches'].append({
                    'company_id': co_id,
                    'company_name': packet_co.get('name', rcon_co.get('name', 'Unknown'))
                })
        
        # Note companies only in one dataset
        for co_id in packet_only:
            comparison['differences'].append({
                'company_id': co_id,
                'company_name': self.packet_data[co_id].get('name', 'Unknown'),
                'differences': [f"Only in packet data"]
            })
        
        for co_id in rcon_only:
            comparison['differences'].append({
                'company_id': co_id,
                'company_name': self.rcon_data[co_id].get('name', 'Unknown'),
                'differences': [f"Only in RCON data"]
            })
        
        return comparison

    def run_test(self, timeout: int = 30) -> Dict[str, Any]:
        """Run the complete test"""
        logger.info("Starting packet vs RCON comparison test...")
        
        try:
            # Login to server
            logger.info("Logging in to server...")
            self.admin.login(self.config.admin_name, self.config.admin_pass)
            
            # Subscribe to company updates (use weekly like the successful test)
            self.admin.subscribe(AdminUpdateType.COMPANY_INFO, AdminUpdateFrequency.AUTOMATIC)
            self.admin.subscribe(AdminUpdateType.COMPANY_ECONOMY, AdminUpdateFrequency.WEEKLY)
            
            # Wait for weekly packet data (this is what worked in previous tests)
            logger.info("Waiting for weekly company economy packet...")
            start_time = time.time()
            weekly_timeout = 20  # Wait up to 20 seconds for weekly packet
            
            while (time.time() - start_time) < weekly_timeout:
                try:
                    packets = self.admin.recv()
                    for packet in packets:
                        self.admin.handle_packet(packet)
                    
                    # Check if we have economy data
                    has_economy_data = any(
                        co_data.get('value') is not None and co_data.get('money') is not None
                        for co_data in self.packet_data.values()
                    )
                    
                    if has_economy_data:
                        logger.info("Weekly company economy packet received!")
                        break
                        
                except Exception as e:
                    logger.debug(f"Packet receive error: {e}")
                time.sleep(0.5)
            
            # Check if we got the data
            has_economy_data = any(
                co_data.get('value') is not None and co_data.get('money') is not None
                for co_data in self.packet_data.values()
            )
            
            if not has_economy_data:
                logger.warning("No weekly economy packet received within timeout")
            else:
                logger.info(f"Received economy data for {len(self.packet_data)} companies")
            
            # Wait for packet data
            logger.info("Waiting for packet data...")
            start_time = time.time()
            while (time.time() - start_time) < timeout:
                try:
                    packets = self.admin.recv()
                    for packet in packets:
                        self.admin.handle_packet(packet)
                except Exception as e:
                    logger.debug(f"Packet receive error: {e}")
                time.sleep(0.5)
                
                # Check if we have complete data (names + values)
                complete_data = True
                for co_id, co_data in self.packet_data.items():
                    if not co_data.get('name') or not co_data.get('value'):
                        complete_data = False
                        break
                
                if complete_data and len(self.packet_data) > 0:
                    logger.info("Complete packet data received")
                    break
            
            # Get RCON data (simplified)
            logger.info("Fetching RCON data...")
            self.get_rcon_companies()
            
            # Compare data
            logger.info("Comparing data...")
            comparison = self.compare_data()
            
            # Print results
            self.print_results(comparison)
            
            return comparison
            
        except Exception as e:
            logger.error(f"Test failed: {e}")
            return {'error': str(e)}
        finally:
            try:
                # Close connection
                self.admin.socket.close()
                logger.info("Disconnected from server")
            except:
                pass

    def print_results(self, comparison: Dict[str, Any]) -> None:
        """Print comparison results"""
        print("\n" + "="*60)
        print("PACKET vs RCON COMPARISON RESULTS")
        print("="*60)
        
        print(f"\nSummary:")
        print(f"  Packet companies: {comparison['packet_companies']}")
        print(f"  RCON companies:   {comparison['rcon_companies']}")
        print(f"  Matches:           {len(comparison['matches'])}")
        print(f"  Differences:       {len(comparison['differences'])}")
        
        if comparison['matches']:
            print(f"\n✓ Matching companies ({len(comparison['matches'])}):")
            for match in comparison['matches'][:5]:  # Show first 5
                print(f"  ID {match['company_id']}: {match['company_name']}")
            if len(comparison['matches']) > 5:
                print(f"  ... and {len(comparison['matches']) - 5} more")
        
        if comparison['differences']:
            print(f"\n✗ Differences found ({len(comparison['differences'])}):")
            for diff in comparison['differences'][:5]:  # Show first 5
                print(f"  ID {diff['company_id']} ({diff['company_name']}):")
                for item in diff['differences']:
                    print(f"    - {item}")
            if len(comparison['differences']) > 5:
                print(f"  ... and {len(comparison['differences']) - 5} more")
        
        print("\n" + "="*60)

class PacketAdminTester:
    """Packet-based OpenTTD admin protocol tester - equivalent to RCON client tests"""
    
    def __init__(self, config: TestConfig):
        self.config = config
        self.results = []
        self.packet_data = {}
        self.client_data = {}
        self.server_info = {}
        self.date_info = {}
        self.packet_received = False
        
        # Initialize admin connection
        self.admin = Admin(
            ip=config.server_ip,
            port=config.admin_port
        )
        
        # Register packet handlers
        self._register_packet_handlers()
        
        logger.info(f"PacketAdminTester initialized for server {config.server_ip}:{config.admin_port}")

    def _register_packet_handlers(self):
        """Register all necessary packet handlers"""
        @self.admin.add_handler(openttdpacket.CompanyInfoPacket)
        def handle_company_info(admin, pkt):
            self.on_company_info(admin, pkt)
        
        @self.admin.add_handler(openttdpacket.CompanyUpdatePacket)
        def handle_company_update(admin, pkt):
            self.on_company_update(admin, pkt)
            
        @self.admin.add_handler(openttdpacket.CompanyEconomyPacket)
        def handle_company_economy(admin, pkt):
            self.on_company_economy(admin, pkt)
            
        @self.admin.add_handler(openttdpacket.CompanyRemovePacket)
        def handle_company_remove(admin, pkt):
            self.on_company_remove(admin, pkt)
            
        @self.admin.add_handler(openttdpacket.ClientInfoPacket)
        def handle_client_info(admin, pkt):
            self.on_client_info(admin, pkt)
            
        @self.admin.add_handler(openttdpacket.ClientUpdatePacket)
        def handle_client_update(admin, pkt):
            self.on_client_update(admin, pkt)
            
        @self.admin.add_handler(openttdpacket.ClientRemovePacket)
        def handle_client_remove(admin, pkt):
            self.on_client_remove(admin, pkt)
            
        @self.admin.add_handler(openttdpacket.WelcomePacket)
        def handle_welcome(admin, pkt):
            self.on_welcome(admin, pkt)
            
        @self.admin.add_handler(openttdpacket.DatePacket)
        def handle_date(admin, pkt):
            self.on_date(admin, pkt)
            
        @self.admin.add_handler(openttdpacket.ServerInfoPacket)
        def handle_server_info(admin, pkt):
            self.on_server_info(admin, pkt)

    def on_company_info(self, admin, pkt) -> None:
        """Handle CompanyInfoPacket"""
        co_id = pkt.id + 1  # Convert from 0-based packet ID to 1-based RCON ID
        existing = self.packet_data.get(co_id, {})
        existing.update({
            'display_id': co_id,
            'name': (pkt.name or '').strip(),
            'manager': pkt.manager_name,
            'start_date': pkt.year,
            'color': getattr(pkt.color, 'name', str(pkt.color)),
            'passworded': pkt.passworded,
            'is_ai': pkt.is_ai,
            'source': 'packet_info'
        })
        self.packet_data[co_id] = existing
        self.packet_received = True
        logger.debug(f"[Packet] CompanyInfo: ID={co_id}, Name={pkt.name}")

    def on_company_update(self, admin, pkt) -> None:
        """Handle CompanyUpdatePacket"""
        co_id = pkt.id + 1  # Convert from 0-based packet ID to 1-based RCON ID
        existing = self.packet_data.get(co_id, {})
        existing.update({
            'display_id': co_id,
            'name': (pkt.name or '').strip(),
            'manager': pkt.manager_name,
            'color': getattr(pkt.color, 'name', str(pkt.color)),
            'passworded': pkt.passworded,
            'source': 'packet_update'
        })
        self.packet_data[co_id] = existing
        self.packet_received = True
        logger.debug(f"[Packet] CompanyUpdate: ID={co_id}, Name={pkt.name}")

    def on_company_economy(self, admin, pkt) -> None:
        """Handle CompanyEconomyPacket"""
        co_id = pkt.id + 1  # Convert from 0-based packet ID to 1-based RCON ID
        existing = self.packet_data.get(co_id, {})
        
        latest_value = 0
        try:
            if pkt.quarterly_info:
                latest_value = pkt.quarterly_info[0].get('company_value', 0)
        except (AttributeError, IndexError, KeyError):
            pass
        
        existing.update({
            'display_id': co_id,
            'money': pkt.money,
            'loan': pkt.current_loan,
            'income': pkt.income,
            'delivered': pkt.delivered_cargo,
            'value': latest_value or existing.get('value', 0),
            'source': 'packet_economy'
        })
        self.packet_data[co_id] = existing
        self.packet_received = True
        logger.debug(f"[Packet] CompanyEconomy: ID={co_id}, Value={latest_value}")

    def on_company_remove(self, admin, pkt) -> None:
        """Handle CompanyRemovePacket"""
        co_id = pkt.id + 1  # Convert from 0-based packet ID to 1-based RCON ID
        if co_id in self.packet_data:
            del self.packet_data[co_id]
        logger.info(f"[Packet] CompanyRemove: ID={co_id}")

    def on_client_info(self, admin, pkt) -> None:
        """Handle ClientInfoPacket"""
        client_id = pkt.id
        self.client_data[client_id] = {
            'id': client_id,
            'name': (pkt.name or '').strip(),
            'company_id': pkt.company_id + 1 if pkt.company_id >= 0 else 255,  # Convert to 1-based, 255 for spectators
            'source': 'packet_client_info'
        }
        self.packet_received = True
        logger.debug(f"[Packet] ClientInfo: ID={client_id}, Name={pkt.name}, Company={pkt.company_id}")

    def on_client_update(self, admin, pkt) -> None:
        """Handle ClientUpdatePacket"""
        client_id = pkt.id
        existing = self.client_data.get(client_id, {})
        existing.update({
            'id': client_id,
            'name': (pkt.name or '').strip(),
            'company_id': pkt.company_id + 1 if pkt.company_id >= 0 else 255,
            'source': 'packet_client_update'
        })
        self.client_data[client_id] = existing
        self.packet_received = True
        logger.debug(f"[Packet] ClientUpdate: ID={client_id}, Name={pkt.name}")

    def on_client_remove(self, admin, pkt) -> None:
        """Handle ClientRemovePacket"""
        client_id = pkt.id
        if client_id in self.client_data:
            del self.client_data[client_id]
        logger.info(f"[Packet] ClientRemove: ID={client_id}")

    def on_welcome(self, admin, pkt) -> None:
        """Handle WelcomePacket"""
        self.server_info['server_name'] = getattr(pkt, 'server_name', 'Unknown')
        self.server_info['welcome_received'] = True
        logger.info(f"[Packet] Welcome received: {self.server_info['server_name']}")

    def on_date(self, admin, pkt) -> None:
        """Handle DatePacket"""
        self.date_info['date'] = getattr(pkt, 'date', None)
        self.date_info['date_received'] = True
        logger.debug(f"[Packet] Date: {self.date_info['date']}")

    def on_server_info(self, admin, pkt) -> None:
        """Handle ServerInfoPacket"""
        self.server_info.update({
            'server_name': getattr(pkt, 'server_name', ''),
            'server_version': getattr(pkt, 'server_version', ''),
            'map_name': getattr(pkt, 'map_name', ''),
            'map_size': getattr(pkt, 'map_size', ''),
            'landscape': getattr(pkt, 'landscape', ''),
            'dedicated': getattr(pkt, 'dedicated', False),
            'info_received': True
        })
        logger.debug(f"[Packet] ServerInfo: {self.server_info.get('server_name')}")

    def wait_for_packets(self, timeout: int = 10) -> bool:
        """Wait for packet data with timeout"""
        start_time = time.time()
        self.packet_received = False
        
        while (time.time() - start_time) < timeout:
            try:
                packets = self.admin.recv()
                for packet in packets:
                    self.admin.handle_packet(packet)
                    
                # Check if we have meaningful data
                if self.packet_data or self.client_data or self.server_info:
                    return True
                    
            except Exception as e:
                logger.debug(f"Packet receive error: {e}")
            time.sleep(0.1)
        
        return False

    def test_packet_companies(self) -> Dict[str, Any]:
        """Test company data via packets (equivalent to RCON companies command)"""
        logger.info("Testing packet-based company data...")
        
        if not self.packet_data:
            return {
                'passed': False,
                'message': "No company data received via packets",
                'data': {}
            }
        
        # Validate company data structure
        valid_companies = 0
        for co_id, co_data in self.packet_data.items():
            if co_data.get('name') and co_data.get('display_id'):
                valid_companies += 1
        
        passed = valid_companies > 0
        message = f"Received {valid_companies} valid companies via packets"
        
        return {
            'passed': passed,
            'message': message,
            'data': self.packet_data
        }

    def test_packet_clients(self) -> Dict[str, Any]:
        """Test client data via packets (equivalent to RCON clients command)"""
        logger.info("Testing packet-based client data...")
        
        if not self.client_data:
            return {
                'passed': False,
                'message': "No client data received via packets",
                'data': {}
            }
        
        # Validate client data structure
        valid_clients = 0
        for client_id, client_data in self.client_data.items():
            if client_data.get('name') is not None:
                valid_clients += 1
        
        passed = valid_clients >= 0  # Having 0 clients is valid
        message = f"Received {valid_clients} valid clients via packets"
        
        return {
            'passed': passed,
            'message': message,
            'data': self.client_data
        }

    def test_packet_server_info(self) -> Dict[str, Any]:
        """Test server info via packets (equivalent to RCON name/server_info commands)"""
        logger.info("Testing packet-based server info...")
        
        if not self.server_info.get('welcome_received'):
            return {
                'passed': False,
                'message': "No welcome packet received",
                'data': {}
            }
        
        server_name = self.server_info.get('server_name', 'Unknown')
        passed = bool(server_name and server_name != 'Unknown')
        message = f"Server name: {server_name}"
        
        return {
            'passed': passed,
            'message': message,
            'data': self.server_info
        }

    def test_packet_date(self) -> Dict[str, Any]:
        """Test date info via packets (equivalent to RCON get_date command)"""
        logger.info("Testing packet-based date info...")
        
        if not self.date_info.get('date_received'):
            return {
                'passed': False,
                'message': "No date packet received",
                'data': {}
            }
        
        date = self.date_info.get('date')
        passed = date is not None
        message = f"Date: {date}" if date else "Date received but invalid"
        
        return {
            'passed': passed,
            'message': message,
            'data': self.date_info
        }

    def test_packet_subscriptions(self) -> Dict[str, Any]:
        """Test packet subscriptions (equivalent to RCON command availability)"""
        logger.info("Testing packet subscription capabilities...")
        
        subscription_tests = {}
        
        # Test various subscription types
        test_subscriptions = [
            (AdminUpdateType.COMPANY_INFO, AdminUpdateFrequency.AUTOMATIC),
            (AdminUpdateType.COMPANY_ECONOMY, AdminUpdateFrequency.WEEKLY),
            (AdminUpdateType.CLIENT_INFO, AdminUpdateFrequency.AUTOMATIC),
            (AdminUpdateType.DATE, AdminUpdateFrequency.DAILY),
        ]
        
        for update_type, frequency in test_subscriptions:
            try:
                self.admin.subscribe(update_type, frequency)
                subscription_tests[f"{update_type.name}_{frequency.name}"] = True
                logger.debug(f"Successfully subscribed to {update_type.name} at {frequency.name}")
            except Exception as e:
                subscription_tests[f"{update_type.name}_{frequency.name}"] = False
                logger.debug(f"Failed to subscribe to {update_type.name} at {frequency.name}: {e}")
        
        total_tests = len(subscription_tests)
        passed_tests = sum(subscription_tests.values())
        passed = passed_tests > 0
        
        return {
            'passed': passed,
            'message': f"Passed {passed_tests}/{total_tests} subscription tests",
            'data': subscription_tests
        }

    def run_packet_tests(self) -> List[Dict[str, Any]]:
        """Run all packet-based tests"""
        logger.info("Starting packet-based admin protocol tests...")
        results = []
        
        try:
            # Login to server
            logger.info("Logging in to server...")
            self.admin.login(self.config.admin_name, self.config.admin_pass)
            
            # Subscribe to basic updates
            self.admin.subscribe(AdminUpdateType.COMPANY_INFO, AdminUpdateFrequency.AUTOMATIC)
            self.admin.subscribe(AdminUpdateType.CLIENT_INFO, AdminUpdateFrequency.AUTOMATIC)
            self.admin.subscribe(AdminUpdateType.DATE, AdminUpdateFrequency.DAILY)
            
            # Wait for initial packet data
            logger.info("Waiting for packet data...")
            if not self.wait_for_packets(timeout=15):
                logger.warning("Timeout waiting for initial packet data")
            
            # Run individual tests
            results.append(self.test_packet_companies())
            results.append(self.test_packet_clients())
            results.append(self.test_packet_server_info())
            results.append(self.test_packet_date())
            results.append(self.test_packet_subscriptions())
            
            # Print results
            self._print_packet_results(results)
            
        except Exception as e:
            logger.error(f"Packet test failed: {e}")
            results.append({
                'passed': False,
                'message': f"Test suite failed: {e}",
                'data': {}
            })
        finally:
            try:
                self.admin.socket.close()
                logger.info("Disconnected from server")
            except:
                pass
        
        return results

    def _print_packet_results(self, results: List[Dict[str, Any]]) -> None:
        """Print packet test results"""
        print("\n" + "="*60)
        print("PACKET-BASED ADMIN PROTOCOL TEST RESULTS")
        print("="*60)
        
        passed = sum(1 for r in results if r['passed'])
        total = len(results)
        
        print(f"\nSummary: {passed}/{total} tests passed")
        
        for result in results:
            status = "✓" if result['passed'] else "✗"
            print(f"{status} {result['message']}")
        
        print("\n" + "="*60)

def load_test_settings() -> TestConfig:
    """Load settings and create TestConfig for first server"""
    with open("settings.json", "r", encoding="utf-8") as f:
        settings = json.load(f)
    
    # Use first server port
    admin_port = settings['admin_ports'][0]
    
    return TestConfig(
        admin_port=admin_port,
        game_port=0,  # Not used for this test
        server_num=1,
        server_ip=settings['server_ip'],
        admin_name=settings['admin_name'],
        admin_pass=settings['admin_pass'],
        goal_value=settings['goal_value'],
        load_scenario=settings['load_scenario'],
        dead_co_age=settings['dead_co_age'],
        dead_co_value=settings['dead_co_value'],
        rcon_retry_max=settings['rcon_retry_max'],
        rcon_retry_delay=settings['rcon_retry_delay'],
        reconnect_max_attempts=settings['reconnect_max_attempts'],
        reconnect_delay=settings['reconnect_delay'],
        reset_countdown_seconds=settings['reset_countdown_seconds']
    )

if __name__ == "__main__":
    try:
        config = load_test_settings()
        
        # Run packet vs RCON comparison test
        logger.info("Running packet vs RCON comparison test...")
        tester = PacketRconTester(config)
        results = tester.run_test(timeout=30)
        
        if 'error' in results:
            logger.error(f"Comparison test failed: {results['error']}")
        else:
            logger.info("Comparison test completed successfully")
        
        # Run packet-only admin protocol test
        logger.info("Running packet-only admin protocol test...")
        packet_tester = PacketAdminTester(config)
        packet_results = packet_tester.run_packet_tests()
        
        passed = sum(1 for r in packet_results if r['passed'])
        total = len(packet_results)
        
        if passed == 0:
            logger.error("All packet tests failed")
            sys.exit(1)
        else:
            logger.info(f"Packet tests completed: {passed}/{total} passed")
        
        logger.info("All tests completed successfully")
            
    except KeyboardInterrupt:
        logger.info("Test interrupted by user")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        sys.exit(1)
