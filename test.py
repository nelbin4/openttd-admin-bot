#!/usr/bin/env python3
"""
OpenTTD Admin - Packet Company and Client Info
"""

import json
import logging
import sys
import time
import math
from datetime import date, timedelta
from typing import Dict, Any

# Try to import the library, handle failure gracefully
try:
    from pyopenttdadmin import *
except ImportError:
    sys.path.insert(0, '/home/nelbin/openttd-admin/reference/pyOpenTTDAdmin')
    try:
        from pyopenttdadmin import *
    except ImportError:
        print("Error: 'pyopenttdadmin' library not found.")
        sys.exit(1)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

companies: Dict[int, Dict[str, Any]] = {}
clients: Dict[int, Dict[str, Any]] = {}
game_date: int | None = None
server_info: Dict[str, Any] = {}
data_received = False

def ottd_date_to_year(day_count: int) -> int:
    # 365.2425 days per year on average
    return math.floor(day_count / 365.2425)

def load_settings(path: str = "settings.json") -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {
            "server_ip": "127.0.0.1",
            "admin_ports": [3977],
            "admin_name": "python_admin",
            "admin_pass": "password"
        }

def send_poll(admin: Admin, update_type: int, data: int):
    # Packet: [Size (2)][Type (1)][UpdateType (1)][Data (4)]
    PACKET_TYPE = 3  # ADMIN_PACKET_ADMIN_POLL
    payload = update_type.to_bytes(1, 'little') + data.to_bytes(4, 'little')
    packet_size = 3 + len(payload)
    packet = packet_size.to_bytes(2, 'little') + PACKET_TYPE.to_bytes(1, 'little') + payload
    admin.socket.sendall(packet)

def display_collected_data():
    print("\n" + "="*100)
    print(f"SERVER: {server_info.get('name', 'Unknown')}")
    
    if game_date is not None:
        print(f"Game Year {ottd_date_to_year(game_date)} (Packet: {game_date})")
    
    print(f"Companies ({len(companies)}):")
    for company_id, company in sorted(companies.items()):
        value_str = f"£{company['value']:,}" if 'value' in company else "N/A"
        print(f"  [{company_id}] {company.get('name', 'N/A')} | Manager: {company.get('manager', 'N/A')} | Value: {value_str}")
    
    print(f"Clients ({len(clients)}):")
    for client_id, client in sorted(clients.items()):
        cid = client.get('company_id', 255)
        if cid == 255:
            role = "Spectator"
        else:
            c_name = companies.get(cid, {}).get('name', f'Company {cid}')
            role = f"Playing as '{c_name}' (#{cid})"

        joined_raw = client.get('join_date', 'N/A')
        if isinstance(joined_raw, int):
            joined_str = f"Year {ottd_date_to_year(joined_raw)}"
        else:
            joined_str = joined_raw

        print(f"  [{client_id}] {client.get('name', 'N/A')} | {role} | Joined: {joined_str} | IP: {client.get('ip', 'Hidden')}")
    
    print("="*100)

def main():
    admin = None
    try:
        settings = load_settings()
        server_ip = settings.get("server_ip")
        admin_port = 3976
        admin_name = settings.get("admin_name", "admin")
        admin_pass = settings.get("admin_pass", "")
        
        logger.info(f"Connecting to {server_ip}:{admin_port} as '{admin_name}'")
        admin = Admin(ip=server_ip, port=admin_port)
        
        # --- Packet Handlers ---
        @admin.add_handler(openttdpacket.WelcomePacket)
        def handle_welcome(_admin, packet):
            logger.info(f"Connected to Server: {packet.server_name}")
            server_info['name'] = packet.server_name
            server_info['version'] = packet.version

        @admin.add_handler(openttdpacket.DatePacket)
        def handle_date(_admin, packet):
            global game_date, data_received
            game_date = packet.date
            data_received = True

        @admin.add_handler(openttdpacket.CompanyInfoPacket)
        def handle_company_info(_admin, packet):
            cid = packet.id
            companies[cid] = {
                'id': cid,
                'name': packet.name,
                'manager': packet.manager_name,
                'is_ai': packet.is_ai
            }
            logger.info(f"Received Info for Company #{cid}")

        @admin.add_handler(openttdpacket.CompanyEconomyPacket)
        def handle_company_economy(_admin, packet):
            cid = packet.id
            if cid not in companies: companies[cid] = {'id': cid}
            companies[cid]['money'] = packet.money
            if hasattr(packet, 'quarterly_info') and packet.quarterly_info:
                last = packet.quarterly_info[-1]
                companies[cid]['value'] = last['company_value']

        @admin.add_handler(openttdpacket.ClientInfoPacket)
        def handle_client_info(_admin, packet):
            cid = packet.id
            # Handle potential differences in library attribute names
            play_as = getattr(packet, 'play_as', getattr(packet, 'company_id', 255))
            ip_addr = getattr(packet, 'ip', 'N/A')
            
            clients[cid] = {
                'id': cid,
                'name': packet.name,
                'ip': ip_addr,
                'join_date': packet.joined,
                'company_id': play_as
            }
            logger.info(f"Client Info: #{cid} {packet.name}")

        # Attempt to handle Client Update if the library supports the packet type
        # (This handler is valid, but the subscription type for it is just CLIENT_INFO)
        if hasattr(openttdpacket, 'ClientUpdatePacket'):
            @admin.add_handler(openttdpacket.ClientUpdatePacket)
            def handle_client_update(_admin, packet):
                cid = packet.id
                if cid in clients:
                    clients[cid]['name'] = packet.name
                    clients[cid]['company_id'] = packet.play_as
                    logger.info(f"Client Update: #{cid} is now {packet.name}")

        @admin.add_handler(openttdpacket.ClientErrorPacket)
        def handle_error(_admin, packet):
            logger.error(f"Server Error: {packet.error}")

        # --- Connection Sequence ---
        admin.login(admin_name, admin_pass)
        
        # Subscribe to updates (CLIENT_INFO covers updates and quits)
        update_types = [
            AdminUpdateType.DATE,
            AdminUpdateType.CLIENT_INFO,
            AdminUpdateType.COMPANY_INFO,
            AdminUpdateType.COMPANY_ECONOMY,
        ]
        
        for ut in update_types:
            admin.subscribe(ut, AdminUpdateFrequency.POLL)
            # Some servers require explicit AUTOMATIC subscription for live updates
            # But POLL is usually safer for a one-shot script
            # admin.subscribe(ut, AdminUpdateFrequency.AUTOMATIC)

        logger.info("Sending Polls...")
        send_poll(admin, AdminUpdateType.DATE.value, 0)
        send_poll(admin, AdminUpdateType.CLIENT_INFO.value, 0xFFFFFFFF)
        for cid in range(16):
            send_poll(admin, AdminUpdateType.COMPANY_INFO.value, cid)
            send_poll(admin, AdminUpdateType.COMPANY_ECONOMY.value, cid)
        
        logger.info("Collecting data (up to 5 seconds)...")
        start_time = time.time()
        last_packet_ts = start_time
        while time.time() - start_time < 5:
            try:
                packets = admin.recv()
                if packets:
                    for packet in packets:
                        admin.handle_packet(packet)
                    last_packet_ts = time.time()
                # Exit early if we've gone quiet after getting packets
                if data_received and (time.time() - last_packet_ts) > 0.3:
                    break
                time.sleep(0.05)
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"Connection error: {e}")
                break
        
        display_collected_data()
        
    except Exception as e:
        logger.error(f"Fatal Error: {e}", exc_info=True)
    finally:
        # Fixed: Check for socket existence directly
        if admin and hasattr(admin, 'socket'):
            try:
                admin.socket.close()
                logger.info("Connection Closed")
            except Exception as e:
                logger.error(f"Error closing socket: {e}")

if __name__ == "__main__":
    main()
