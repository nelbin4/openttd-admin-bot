#!/usr/bin/env python3

import logging
import sys
import time
from datetime import date, timedelta
from typing import Dict, Any
import getpass

ip = "127.0.0.1"
port = 3977
password = "PASSWORDPASSWORD"

try:
    from pyopenttdadmin import *
except ImportError:
    sys.path.insert(0, '/home/nelbin/openttd-admin/reference/pyOpenTTDAdmin')
    from pyopenttdadmin import *

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

companies: Dict[int, Dict[str, Any]] = {}
clients: Dict[int, Dict[str, Any]] = {}
game_date: int | None = None
server_info: Dict[str, Any] = {}
data_received = False

def ottd_date_to_year(day_count: int) -> int:
    return (date(1, 1, 1) + timedelta(days=day_count)).year - 1

def send_poll(admin: Admin, update_type: int, data: int):
    PACKET_TYPE = 3
    payload = update_type.to_bytes(1, 'little') + data.to_bytes(4, 'little')
    packet_size = 3 + len(payload)
    packet = packet_size.to_bytes(2, 'little') + PACKET_TYPE.to_bytes(1, 'little') + payload
    admin.socket.sendall(packet)

def display_collected_data():
    print("\n" + "="*30)
    header_parts = [f"SERVER: {server_info.get('name', 'Unknown')}", f"IP:PORT {ip}:{port}"]
    if game_date is not None:
        header_parts.append(f"Game Year {ottd_date_to_year(game_date)}")
    print(" | ".join(header_parts))
    
    print(f"Companies ({len(companies)}):")
    for company_id, company in sorted(companies.items()):
        display_id = company_id + 1
        value_str = f"£{company['value']:,}" if 'value' in company else "N/A"
        founded = company.get('founded')
        if isinstance(founded, int):
            founded_str = f"Year {founded}"
        else:
            founded_str = "Year N/A"
        print(f"  [{display_id}] {company.get('name', 'N/A')} | {founded_str} | Value: {value_str}")

    print(f"Clients ({len(clients)}):")
    for client_id, client in sorted(clients.items()):
        cid = client.get('company_id', 255)
        if cid == 255:
            role = "Spectator"
        else:
            display_id = cid + 1
            c_name = companies.get(cid, {}).get('name', f'Company {display_id}')
            role = f"Playing as '{c_name}' (#{display_id})"

        print(f"  [{client_id}] {client.get('name', 'N/A')} | {role} | IP: {client.get('ip', 'Hidden')}")
    
    print("="*30)

admin = None
try:
    server_ip = ip or input("Server IP: ").strip()
    if not server_ip:
        logger.error("Server IP is required.")
        sys.exit(1)

    admin_port = port if port else 3976
    admin_name = "Admin"
    admin_pass = password or getpass.getpass("Admin Password: ")

    globals()['ip'] = server_ip
    globals()['port'] = admin_port

    logger.info(f"Connecting to {server_ip}:{admin_port} as '{admin_name}'")
    admin = Admin(ip=server_ip, port=admin_port)

    @admin.add_handler(openttdpacket.WelcomePacket)
    def handle_welcome(_admin, packet):
        logger.info(f"Connected to server")
        server_info['name'] = packet.server_name

    @admin.add_handler(openttdpacket.DatePacket)
    def handle_date(_admin, packet):
        global game_date, data_received
        game_date = packet.date
        data_received = True

    @admin.add_handler(openttdpacket.CompanyInfoPacket)
    def handle_company_info(_admin, packet):
        cid = packet.id
        founded = packet.year if hasattr(packet, 'year') and isinstance(packet.year, int) else None
        companies[cid] = {
            'id': cid,
            'name': packet.name,
            'founded': founded
        }
        logger.info(f"Received Info for Company #{cid}")

    @admin.add_handler(openttdpacket.CompanyEconomyPacket)
    def handle_company_economy(_admin, packet):
        cid = packet.id
        if cid not in companies:
            companies[cid] = {'id': cid}
        companies[cid]['money'] = packet.money
        if hasattr(packet, 'quarterly_info') and packet.quarterly_info:
            last = packet.quarterly_info[-1]
            companies[cid]['value'] = last['company_value']

    @admin.add_handler(openttdpacket.ClientInfoPacket)
    def handle_client_info(_admin, packet):
        cid = packet.id
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

    admin.login(admin_name, admin_pass)

    update_types = [
        AdminUpdateType.DATE,
        AdminUpdateType.CLIENT_INFO,
        AdminUpdateType.COMPANY_INFO,
        AdminUpdateType.COMPANY_ECONOMY,
    ]
    for ut in update_types:
        admin.subscribe(ut, AdminUpdateFrequency.POLL)

    logger.info("Sending Polls...")
    send_poll(admin, AdminUpdateType.DATE.value, 0)
    send_poll(admin, AdminUpdateType.CLIENT_INFO.value, 0xFFFFFFFF)
    for cid in range(16):
        send_poll(admin, AdminUpdateType.COMPANY_INFO.value, cid)
        send_poll(admin, AdminUpdateType.COMPANY_ECONOMY.value, cid)

    logger.info("Collecting data")
    start_time = time.time()
    last_packet_ts = start_time
    while time.time() - start_time < 5:
        try:
            packets = admin.recv()
            if packets:
                for packet in packets:
                    admin.handle_packet(packet)
                last_packet_ts = time.time()
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
    if admin and hasattr(admin, 'socket'):
        try:
            admin.socket.close()
            logger.info("Connection Closed")
        except Exception as e:
            logger.error(f"Error closing socket: {e}")
