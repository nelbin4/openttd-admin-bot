#!/usr/bin/env python3

import logging
import sys
import time
from datetime import date, timedelta
from typing import Dict, Any

from pyopenttdadmin import *

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)


class OpenTTDMonitor:
    """Monitor for OpenTTD server status, companies, and clients."""
    
    # Configuration
    SERVER_IP = "127.0.0.1"
    SERVER_PORT = 3977
    ADMIN_PASSWORD = "PASSWORDPASSWORD"
    ADMIN_NAME = "Admin"
    COLLECTION_TIMEOUT = 5
    
    def __init__(self):
        self.companies: Dict[int, Dict[str, Any]] = {}
        self.clients: Dict[int, Dict[str, Any]] = {}
        self.game_date: int | None = None
        self.server_name: str = "Unknown"
        self.admin: Admin | None = None
        self.data_received = False
    
    @staticmethod
    def ottd_date_to_year(day_count: int) -> int:
        """Convert OpenTTD day count to year."""
        return (date(1, 1, 1) + timedelta(days=day_count)).year - 1
    
    def send_poll(self, update_type: int, data: int):
        """Send a poll request to the server."""
        PACKET_TYPE = 3
        payload = update_type.to_bytes(1, 'little') + data.to_bytes(4, 'little')
        packet_size = 3 + len(payload)
        packet = packet_size.to_bytes(2, 'little') + PACKET_TYPE.to_bytes(1, 'little') + payload
        self.admin.socket.sendall(packet)
    
    def setup_handlers(self):
        """Register packet handlers."""
        
        @self.admin.add_handler(openttdpacket.WelcomePacket)
        def handle_welcome(_admin, packet):
            self.server_name = packet.server_name
            logger.info(f"Connected to server")
        
        @self.admin.add_handler(openttdpacket.DatePacket)
        def handle_date(_admin, packet):
            self.game_date = packet.date
            self.data_received = True
        
        @self.admin.add_handler(openttdpacket.CompanyInfoPacket)
        def handle_company_info(_admin, packet):
            founded = getattr(packet, 'year', None) if isinstance(getattr(packet, 'year', None), int) else None
            self.companies[packet.id] = {
                'id': packet.id,
                'name': packet.name,
                'founded': founded
            }
        
        @self.admin.add_handler(openttdpacket.CompanyEconomyPacket)
        def handle_company_economy(_admin, packet):
            if packet.id not in self.companies:
                self.companies[packet.id] = {'id': packet.id}
            
            self.companies[packet.id]['money'] = packet.money
            if hasattr(packet, 'quarterly_info') and packet.quarterly_info:
                self.companies[packet.id]['value'] = packet.quarterly_info[-1]['company_value']
        
        @self.admin.add_handler(openttdpacket.ClientInfoPacket)
        def handle_client_info(_admin, packet):
            self.clients[packet.id] = {
                'id': packet.id,
                'name': packet.name,
                'ip': getattr(packet, 'ip', 'N/A'),
                'join_date': packet.joined,
                'company_id': getattr(packet, 'play_as', getattr(packet, 'company_id', 255))
            }
        
        if hasattr(openttdpacket, 'ClientUpdatePacket'):
            @self.admin.add_handler(openttdpacket.ClientUpdatePacket)
            def handle_client_update(_admin, packet):
                if packet.id in self.clients:
                    self.clients[packet.id].update({
                        'name': packet.name,
                        'company_id': packet.play_as
                    })
    
    def subscribe_and_poll(self):
        """Subscribe to updates and poll for initial data."""
        update_types = [
            AdminUpdateType.DATE,
            AdminUpdateType.CLIENT_INFO,
            AdminUpdateType.COMPANY_INFO,
            AdminUpdateType.COMPANY_ECONOMY,
        ]
        
        for update_type in update_types:
            self.admin.subscribe(update_type, AdminUpdateFrequency.POLL)
        
        logger.info("Polling server for data...")
        self.send_poll(AdminUpdateType.DATE.value, 0)
        self.send_poll(AdminUpdateType.CLIENT_INFO.value, 0xFFFFFFFF)
        
        for company_id in range(16):
            self.send_poll(AdminUpdateType.COMPANY_INFO.value, company_id)
            self.send_poll(AdminUpdateType.COMPANY_ECONOMY.value, company_id)
    
    def collect_data(self):
        """Collect data from the server with timeout."""
        start_time = time.time()
        last_packet_time = start_time
        
        while time.time() - start_time < self.COLLECTION_TIMEOUT:
            try:
                packets = self.admin.recv()
                if packets:
                    for packet in packets:
                        self.admin.handle_packet(packet)
                    last_packet_time = time.time()
                
                # Exit early if we have data and no packets for 300ms
                if self.data_received and (time.time() - last_packet_time) > 0.3:
                    break
                
                time.sleep(0.05)
            
            except KeyboardInterrupt:
                logger.info("Interrupted by user")
                break
            except Exception as e:
                logger.error(f"Error during data collection: {e}")
                break
    
    def display_data(self):
        """Display collected server data."""
        print("\n" + "=" * 50)
        
        # Header
        print(f"Server: {self.server_name}")
        print(f"IP:Port: {self.SERVER_IP}:{self.SERVER_PORT}")
        print(f"Year: {self.ottd_date_to_year(self.game_date) if self.game_date is not None else 'N/A'}")
        
        # Companies
        print(f"\nCompanies ({len(self.companies)}):")
        for company_id, company in sorted(self.companies.items()):
            display_id = company_id + 1
            value = f"£{company['value']:,}" if 'value' in company else "N/A"
            founded = f"Year {company['founded']}" if company.get('founded') else "Year N/A"
            print(f"  [{display_id}] {company.get('name', 'N/A')} | {founded} | Value: {value}")
        
        # Clients
        print(f"\nClients ({len(self.clients)}):")
        for client_id, client in sorted(self.clients.items()):
            company_id = client.get('company_id', 255)
            
            if company_id == 255:
                role = "Spectator"
            else:
                display_id = company_id + 1
                company_name = self.companies.get(company_id, {}).get('name', f'Company {display_id}')
                role = f"Playing as '{company_name}' (#{display_id})"
            
            print(f"  [{client_id}] {client.get('name', 'N/A')} | {role} | IP: {client.get('ip', 'Hidden')}")
        
        print("=" * 50)
    
    def connect(self):
        """Connect to the OpenTTD server."""
        logger.info(f"Connecting to {self.SERVER_IP}:{self.SERVER_PORT}")
        self.admin = Admin(ip=self.SERVER_IP, port=self.SERVER_PORT)
        self.setup_handlers()
        self.admin.login(self.ADMIN_NAME, self.ADMIN_PASSWORD)
    
    def disconnect(self):
        """Close the connection to the server."""
        if self.admin and hasattr(self.admin, 'socket'):
            try:
                self.admin.socket.close()
                logger.info("Connection closed")
            except Exception as e:
                logger.error(f"Error closing socket: {e}")
    
    def run(self):
        """Main execution flow."""
        try:
            self.connect()
            self.subscribe_and_poll()
            self.collect_data()
            self.display_data()
        except Exception as e:
            logger.error(f"Fatal error: {e}", exc_info=True)
        finally:
            self.disconnect()


def main():
    monitor = OpenTTDMonitor()
    monitor.run()


if __name__ == "__main__":
    main()
