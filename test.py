#!/usr/bin/env python3
"""
OpenTTD Admin - Get Company and Client Info

This script connects to an OpenTTD server via the admin port and gathers
company/client details using packet subscriptions. It demonstrates how to
request specific updates (date, company info/economy, client info), process
incoming packets, and print a summarized snapshot. The extra comments explain
the what/how/why for each step so behavior is clear for maintenance.
"""

import logging
import sys
import time
from datetime import date, timedelta
from typing import Dict, Any
import getpass

# --- Config ---
# Target server connection details; adjust to point at your OpenTTD instance.
ip = "192.168.1.10"
port = 3976
password = "PASSWORDPASSWORD"

# Import the admin library from the environment
from pyopenttdadmin import *

# Basic logger so runtime events and errors are visible; DEBUG for maximum detail.
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

# In-memory stores for data collected from the server. Keys are IDs; values are
# dictionaries mirroring packet contents we care about. game_date helps format
# display output, server_info captures metadata shown in the header.
companies: Dict[int, Dict[str, Any]] = {}
clients: Dict[int, Dict[str, Any]] = {}
game_date: int | None = None
server_info: Dict[str, Any] = {}
# Flag toggled after the first packet arrives so the main loop knows when it
# can stop early once traffic quiets down.
data_received = False

# Helper that converts OpenTTD's internal day count (days since year 1) into a
# calendar year by adding the offset to a base date and extracting the year; this
# mirrors how the game tracks time and ensures we present correctly parsed years.
def ottd_date_to_year(day_count: int) -> int:
    """Convert OpenTTD day count to calendar year for readable output."""
    return (date(1, 1, 1) + timedelta(days=day_count)).year - 1

# Helper that sends a raw poll packet to the server to explicitly request a
# specific update type (e.g., date, company info, client info) without relying
# on automatic subscriptions. This ensures we get the data we need on demand.
def send_poll(admin: Admin, update_type: int, data: int):
    """Send an explicit poll packet to request a specific update now.

    The library exposes subscriptions but not ad-hoc poll construction, so we
    build the packet manually following the protocol layout:
    [Size (2 bytes)][Type (1 byte)][UpdateType (1 byte)][Data (4 bytes)].
    """
    PACKET_TYPE = 3  # ADMIN_PACKET_ADMIN_POLL constant from protocol
    payload = update_type.to_bytes(1, 'little') + data.to_bytes(4, 'little')
    packet_size = 3 + len(payload)
    packet = packet_size.to_bytes(2, 'little') + PACKET_TYPE.to_bytes(1, 'little') + payload
    # Push the constructed bytes over the already-authenticated socket.
    admin.socket.sendall(packet)

# Helper that prints a formatted summary of the collected server, company, and
# client data so users can quickly see the current state without scrolling
# through raw logs.
def display_collected_data():
    """Pretty-print the snapshot of server, company, and client data."""
    print("\n" + "="*30)
    # Assemble header elements with server name, IP:port, and game year (if available).
    header_parts = [f"SERVER: {server_info.get('name', 'Unknown')}", f"IP:PORT {ip}:{port}"]
    if game_date is not None:
        # Include the game year in the header for context.
        header_parts.append(f"Game Year {ottd_date_to_year(game_date)}")
    # Join header elements on one line for readability.
    print(" | ".join(header_parts))
    
    # Show companies in ID order with display-friendly numbering (+1).
    print(f"Companies ({len(companies)}):")
    for company_id, company in sorted(companies.items()):
        # Derive a display ID by adding 1 to the internal company ID.
        display_id = company_id + 1
        # Format the company value with commas for readability.
        value_str = f"£{company['value']:,}" if 'value' in company else "N/A"
        # Extract the founded year, falling back to "Year N/A" if missing.
        founded = company.get('founded')
        if isinstance(founded, int):
            founded_str = f"Year {founded}"
        else:
            founded_str = "Year N/A"
        # Print the company details in a consistent format.
        print(f"  [{display_id}] {company.get('name', 'N/A')} | {founded_str} | Value: {value_str}")

    # List clients with their role (spectator vs playing), join year, and IP.
    print(f"Clients ({len(clients)}):")
    for client_id, client in sorted(clients.items()):
        # Determine the client's role based on their company ID (255 = spectator).
        cid = client.get('company_id', 255)
        if cid == 255:
            role = "Spectator"
        else:
            # For players, derive the display ID and company name.
            display_id = cid + 1
            c_name = companies.get(cid, {}).get('name', f'Company {display_id}')
            role = f"Playing as '{c_name}' (#{display_id})"

        joined_raw = client.get('join_date', 'N/A')
        if isinstance(joined_raw, int):
            joined_str = f"Year {ottd_date_to_year(joined_raw)}"
        else:
            joined_str = joined_raw

        print(f"  [{client_id}] {client.get('name', 'N/A')} | {role} | Joined: {joined_str} | IP: {client.get('ip', 'Hidden')}")
    
    print("="*30)

admin = None  # Will hold the Admin connection instance once created.
try:
    # Allow overriding the default IP interactively; empty input is rejected.
    server_ip = ip or input("Server IP: ").strip()
    if not server_ip:
        logger.error("Server IP is required.")
        sys.exit(1)

    # Choose port, display name, and obtain password (prompt if not preset) so
    # the admin client can authenticate properly.
    admin_port = port if port else 3976
    admin_name = "Admin"
    admin_pass = password or getpass.getpass("Admin Password: ")

    # Keep displayed IP/port in sync with connection for the header.
    globals()['ip'] = server_ip
    globals()['port'] = admin_port

    # Instantiate Admin client with the chosen host/port.
    logger.info(f"Connecting to {server_ip}:{admin_port} as '{admin_name}'")
    admin = Admin(ip=server_ip, port=admin_port)

    # --- Packet Handlers ---
    # Each handler updates the in-memory caches when the corresponding packet
    # arrives. Using the decorator keeps routing logic declarative.
    @admin.add_handler(openttdpacket.WelcomePacket)
    def handle_welcome(_admin, packet):
        # Capture server metadata to include in the output header.
        logger.info(f"Connected to server")
        server_info['name'] = packet.server_name
        server_info['version'] = packet.version

    @admin.add_handler(openttdpacket.DatePacket)
    def handle_date(_admin, packet):
        # Track the current game date; also note that we have received data so
        # the main loop can exit once traffic quiets down.
        global game_date, data_received
        game_date = packet.date
        data_received = True

    @admin.add_handler(openttdpacket.CompanyInfoPacket)
    def handle_company_info(_admin, packet):
        # Normalize company info into our cache, deriving a founded year from
        # whichever compatible attribute exists (protocol versions differ).
        cid = packet.id
        founded_year = None
        if hasattr(packet, 'start_year') and isinstance(packet.start_year, int):
            founded_year = packet.start_year
        elif hasattr(packet, 'year') and isinstance(packet.year, int):
            founded_year = packet.year
        elif hasattr(packet, 'inaugurated') and isinstance(packet.inaugurated, int):
            founded_year = ottd_date_to_year(packet.inaugurated)
        elif hasattr(packet, 'year_founded') and isinstance(packet.year_founded, int):
            founded_year = packet.year_founded

        companies[cid] = {
            'id': cid,
            'name': packet.name,
            'is_ai': packet.is_ai,
            'founded': founded_year
        }
        logger.info(f"Received Info for Company #{cid}")

    @admin.add_handler(openttdpacket.CompanyEconomyPacket)
    def handle_company_economy(_admin, packet):
        # Update financials; ensure the company exists in cache before storing
        # money/value. Use the latest quarterly snapshot when available.
        cid = packet.id
        if cid not in companies: companies[cid] = {'id': cid}
        companies[cid]['money'] = packet.money
        if hasattr(packet, 'quarterly_info') and packet.quarterly_info:
            last = packet.quarterly_info[-1]
            companies[cid]['value'] = last['company_value']

    @admin.add_handler(openttdpacket.ClientInfoPacket)
    def handle_client_info(_admin, packet):
        # Cache client identity, join date, and which company they're playing
        # as (255 represents spectator). Fall back IP to 'N/A' if hidden.
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
            # Some protocol versions send name/company changes; update cache so
            # the final display reflects current state.
            cid = packet.id
            if cid in clients:
                clients[cid]['name'] = packet.name
                clients[cid]['company_id'] = packet.play_as
                logger.info(f"Client Update: #{cid} is now {packet.name}")

    @admin.add_handler(openttdpacket.ClientErrorPacket)
    def handle_error(_admin, packet):
        # Bubble up server-side errors for visibility.
        logger.error(f"Server Error: {packet.error}")

    # --- Connection Sequence ---
    # Authenticate, subscribe to relevant feeds with POLL frequency (so we can
    # drive updates manually), then send initial polls to fetch current state.
    # Open the admin connection by authenticating with the server.
    admin.login(admin_name, admin_pass)

    # Subscribe to required update types with POLL frequency to enable manual polling
    update_types = [
        AdminUpdateType.DATE,
        AdminUpdateType.CLIENT_INFO,
        AdminUpdateType.COMPANY_INFO,
        AdminUpdateType.COMPANY_ECONOMY,
    ]
    for ut in update_types:
        admin.subscribe(ut, AdminUpdateFrequency.POLL)

    # Request initial data snapshot from server: current date and all client info.
    # For companies, iterate through possible company IDs (0-15) to fetch both
    # their basic info and economic details.
    logger.info("Sending Polls...")
    send_poll(admin, AdminUpdateType.DATE.value, 0)
    send_poll(admin, AdminUpdateType.CLIENT_INFO.value, 0xFFFFFFFF)
    # Loop through possible company IDs to fetch info/economy snapshots.
    for cid in range(16):
        send_poll(admin, AdminUpdateType.COMPANY_INFO.value, cid)
        send_poll(admin, AdminUpdateType.COMPANY_ECONOMY.value, cid)

    # Wait up to 5 seconds for initial data collection, tracking packet activity
    logger.info("Collecting data")
    start_time = time.time()
    last_packet_ts = start_time
    while time.time() - start_time < 5:
        try:
            # Pull any pending packets; admin.handle_packet dispatches to the
            # handlers above. Track last packet time to know when to stop.
            packets = admin.recv()
            if packets:
                for packet in packets:
                    admin.handle_packet(packet)
                last_packet_ts = time.time()
            if data_received and (time.time() - last_packet_ts) > 0.3:
                # If we've received at least one dataset and things are quiet,
                # exit early instead of waiting the full window.
                break
            time.sleep(0.05)
        except KeyboardInterrupt:
            # Graceful exit on Ctrl+C without stack traces.
            break
        except Exception as e:
            logger.error(f"Connection error: {e}")
            break

    # Once collection window ends, print the aggregated snapshot.
    display_collected_data()

except Exception as e:
    # Surface fatal exceptions with stack trace for debugging.
    logger.error(f"Fatal Error: {e}", exc_info=True)
finally:
    # Always attempt to close the socket to free resources on exit.
    if admin and hasattr(admin, 'socket'):
        try:
            admin.socket.close()
            logger.info("Connection Closed")
        except Exception as e:
            logger.error(f"Error closing socket: {e}")
