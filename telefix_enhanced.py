from typing import Dict
import requests
import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
import json
import os
from dotenv import load_dotenv
import logging
from logging.handlers import RotatingFileHandler
import colorlog
import asyncio
import aiohttp
from datetime import datetime
import sys
import time
import io
import ssl
from threading import Thread
from flask import Flask, jsonify
import concurrent.futures

ssl._create_default_https_context = ssl._create_unverified_context

# Load environment variables
load_dotenv()
API_KEY = os.getenv("SMSVIRTUAL_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
AUTHORIZED_IDS = set(os.getenv("AUTHORIZED_IDS", "").split(","))
ADMIN_IDS = set(os.getenv("ADMIN_IDS", "").split(","))
PORT = int(os.getenv("PORT", 5000))

USER_ID_FILE = "useridbot.txt"

# Create Flask app for health check and UptimeRobot
app = Flask(__name__)


@app.route('/')
def health_check():
    return jsonify({
        "status": "healthy",
        "service": "telegram-bot",
        "timestamp": datetime.now().isoformat(),
        "uptime": "running"
    })


@app.route('/health')
def health():
    return jsonify({"status": "ok"})


@app.route('/ping')
def ping():
    return "pong"


@app.route('/status')
def status():
    return jsonify({
        "bot_status": "running",
        "active_orders": len(order_storage) if 'order_storage' in globals() else 0,
        "monitoring_tasks": len(active_order_monitors) if 'active_order_monitors' in globals() else 0
    })


# Create all file if does not exist
if not os.path.exists("serviceotp.txt"):
    with open("serviceotp.txt", "w", encoding='utf-8') as f:
        pass
if not os.path.exists(".env"):
    with open(".env", "w", encoding='utf-8') as f:
        f.write(
            "SMSVIRTUAL_API_KEY=\nTELEGRAM_BOT_TOKEN=\nAUTHORIZED_IDS=\nADMIN_IDS=\nPORT=5000\n")
if not os.path.exists("bot.log"):
    with open("bot.log", "w", encoding='utf-8') as f:
        pass
if not os.path.exists(USER_ID_FILE):
    with open(USER_ID_FILE, "w", encoding='utf-8') as f:
        pass
if not os.path.exists("logorder.txt"):
    with open("logorder.txt", "w", encoding='utf-8') as f:
        f.write(
            "timestamp,user_id,order_id,service_id,service_name,phone_number,price,status,sms_content\n")

BASE_URL = "https://api.smsvirtual.co/v1/"

# Dictionaries to track users waiting for input
waiting_for_user_id = {}
waiting_for_service_input = {}
waiting_for_service_id = {}
pending_cancellations = {}  # Track orders that need to be cancelled after delay
auto_cancel_timers = {}  # Track 10-minute auto-cancel timers for orders without SMS
active_order_monitors = {}  # Track active order monitoring tasks
user_cancel_requests = {}  # Track user manual cancel requests

# Order storage functions


def save_order_storage():
    """Save order storage to file"""
    try:
        with open("order_storage.json", "w", encoding='utf-8') as f:
            json.dump(order_storage, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to save order storage: {str(e)}")


def load_order_storage():
    """Load order storage from file"""
    global order_storage
    try:
        if os.path.exists("order_storage.json"):
            with open("order_storage.json", "r", encoding='utf-8') as f:
                order_storage = json.load(f)
            logger.info(f"Loaded {len(order_storage)} orders from storage")
        else:
            order_storage = {}
    except Exception as e:
        logger.error(f"Failed to load order storage: {str(e)}")
        order_storage = {}


def store_order_info(order_id: str, service_id: str, service_name: str, phone_number: str, price: float, user_id: str):
    """Store order information"""
    order_storage[order_id] = {
        'service_id': service_id,
        'service_name': service_name,
        'phone_number': phone_number,
        'price': price,
        'order_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'user_id': user_id
    }
    save_order_storage()


def get_order_info(order_id: str) -> dict:
    """Get order information"""
    return order_storage.get(order_id, {
        'service_id': 'N/A',
        'service_name': 'Unknown Service',
        'phone_number': 'N/A',
        'price': 'N/A',
        'order_time': 'N/A',
        'user_id': 'N/A'
    })


# Order storage for maintaining order information
order_storage = {}  # Store order details: {order_id: {service_id, service_name, phone_number, price, order_time, user_id}}

# Enhanced async HTTP session for better performance


class AsyncHTTPClient:
    def __init__(self):
        self.session = None

    async def get_session(self):
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self.session = aiohttp.ClientSession(timeout=timeout)
        return self.session

    async def get(self, url, headers=None):
        session = await self.get_session()
        try:
            async with session.get(url, headers=headers) as response:
                return response.status, await response.json()
        except Exception as e:
            logger.error(f"HTTP GET error: {str(e)}")
            return None, None

    async def post(self, url, data=None, json_data=None, headers=None):
        session = await self.get_session()
        try:
            if json_data:
                async with session.post(url, json=json_data, headers=headers) as response:
                    return response.status, await response.json()
            else:
                async with session.post(url, data=data, headers=headers) as response:
                    return response.status, await response.json()
        except Exception as e:
            logger.error(f"HTTP POST error: {str(e)}")
            return None, None

    async def patch(self, url, data=None, json_data=None, headers=None):
        session = await self.get_session()
        try:
            if json_data:
                async with session.patch(url, json=json_data, headers=headers) as response:
                    return response.status, await response.json()
            else:
                async with session.patch(url, data=data, headers=headers) as response:
                    return response.status, await response.json()
        except Exception as e:
            logger.error(f"HTTP PATCH error: {str(e)}")
            return None, None

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()


# Global HTTP client instance
http_client = AsyncHTTPClient()

# Function to monitor order for SMS updates with enhanced async


async def monitor_order_sms(context: ContextTypes.DEFAULT_TYPE, order_id: str, message_id: int, chat_id: int, initial_sms_count: int = 0):
    """
    Enhanced monitor an order for SMS updates and automatically update the message
    """
    headers = {"X-Api-Key": API_KEY}
    last_sms_count = initial_sms_count
    last_status = None
    check_count = 0
    max_checks = 60  # Monitor for 10 minutes (60 checks * 10 seconds)

    async def check_order_updates():
        nonlocal last_sms_count, last_status, check_count

        while check_count < max_checks:
            try:
                await asyncio.sleep(10)  # Check every 10 seconds
                check_count += 1

                # Check if order is still being monitored
                if order_id not in active_order_monitors:
                    break

                url = f"{BASE_URL}order/status/{order_id}"

                # Use enhanced async HTTP client
                status_code, data = await http_client.get(url, headers)

                if status_code == 200 and data:
                    if data.get('status') and data.get('data'):
                        order_data = data['data']
                        current_status = order_data.get(
                            'orderStatus', 'Unknown')
                        sms_data = order_data.get('Sms', [])
                        current_sms_count = len(sms_data)

                        # Check if there are new SMS or status changes
                        if current_sms_count > last_sms_count or current_status != last_status:
                            last_sms_count = current_sms_count
                            last_status = current_status

                            # Only update if there's actually a change
                            await auto_update_order_message(context, order_id, message_id, chat_id, order_data)

                        # Stop monitoring if order is completed or cancelled
                        if current_status in ['SUCCESS', 'CANCEL', 'REFUND']:
                            break

            except Exception as e:
                logger.error(f"Error monitoring order {order_id}: {str(e)}")
                await asyncio.sleep(5)  # Wait a bit before retrying

        # Remove from active monitors when done
        if order_id in active_order_monitors:
            del active_order_monitors[order_id]

    # Store the monitoring task
    task = asyncio.create_task(check_order_updates())
    active_order_monitors[order_id] = {
        'task': task,
        'message_id': message_id,
        'chat_id': chat_id
    }


def extract_service_info_from_message(message_text: str) -> dict:
    """
    Extract service information from existing message text
    """
    info = {
        'service_name': 'Unknown Service',
        'phone_number': 'N/A',
        'price': 'N/A',
        'order_time': 'N/A',
        'service_id': 'N/A'
    }

    if not message_text:
        return info

    lines = message_text.split('\n')
    for line in lines:
        if line.startswith("✅ Pesanan berhasil"):
            # Extract service name from success message
            info['service_name'] = line.replace(
                "✅ Pesanan berhasil ", "").replace("!", "").strip()
        elif line.startswith("📱 SMS Order") or line.startswith("📱 Status Order") or line.startswith("🔄 Resend Order"):
            # Handle already edited messages
            parts = line.split()
            if len(parts) >= 3:
                info['service_name'] = parts[2]
        elif "📞 Nomor:" in line:
            # Extract phone number, handle HTML code tags
            phone_part = line.replace("📞 Nomor: ", "").strip()
            # Remove HTML code tags and get first phone number
            phone_part = phone_part.replace(
                "<code>", "").replace("</code>", "")
            if " | " in phone_part:
                # Take the first phone number before the pipe
                info['phone_number'] = phone_part.split(" | ")[0].strip()
            else:
                info['phone_number'] = phone_part.strip()
        elif "💵 Harga:" in line:
            info['price'] = line.replace("💵 Harga: ", "").strip()
        elif "🕒 Dipesan pada:" in line:
            info['order_time'] = line.replace("🕒 Dipesan pada: ", "").strip()
        elif "✅ Diselesaikan pada:" in line:
            # Skip this line as it's completion info, not order time
            continue

    return info


async def auto_update_order_message(context: ContextTypes.DEFAULT_TYPE, order_id: str, message_id: int, chat_id: int, order_data: dict):
    """
    Automatically update order message with new SMS data - Enhanced with async
    """
    try:
        order_status = order_data.get('orderStatus', 'Unknown')
        sms_data = order_data.get('Sms', [])
        service_id = str(order_data.get('serviceId', 'N/A'))
        phone_number = order_data.get('number', 'N/A')
        api_price = order_data.get('price', 'N/A')

        # Try to get existing message to preserve service info
        price = f"${float(api_price):.5f}" if api_price != 'N/A' and isinstance(
            api_price, (int, float)) else api_price  # Default price

        # Get stored order info first
        stored_order = get_order_info(order_id)
        service_name = stored_order['service_name']
        order_time = stored_order['order_time']
        stored_phone = stored_order['phone_number']

        # Use stored phone number if API doesn't provide it or if it's better
        if phone_number == 'N/A' or not phone_number:
            phone_number = stored_phone if stored_phone != 'N/A' else phone_number

        try:
            existing_message = await context.bot.get_message(chat_id=chat_id, message_id=message_id)
            service_info = extract_service_info_from_message(
                existing_message.text)

            # Preserve existing service info if better than stored
            if service_info['service_name'] != 'Unknown Service':
                service_name = service_info['service_name']
            if service_info['order_time'] != 'N/A':
                order_time = service_info['order_time']
            if service_info['price'] != 'N/A':
                price = service_info['price']
            # Extract phone number from existing message if current is N/A
            if phone_number == 'N/A' and service_info['phone_number'] != 'N/A':
                phone_number = service_info['phone_number']
        except:
            # Use stored data as fallback
            if stored_order['price'] != 'N/A':
                price = f"${float(stored_order['price']):.5f}" if isinstance(
                    stored_order['price'], (int, float)) else stored_order['price']
            else:
                # Use API price as fallback
                price = f"${float(api_price):.5f}" if api_price != 'N/A' and isinstance(
                    api_price, (int, float)) else api_price

            # If still unknown, try to get from serviceotp.txt
            if service_name == "Unknown Service":
                try:
                    with open("serviceotp.txt", "r", encoding='utf-8') as f:
                        for line in f:
                            parts = line.strip().split(maxsplit=1)
                            if len(parts) == 2 and parts[0] == service_id:
                                service_name = parts[1]
                                break
                except FileNotFoundError:
                    pass

            # Set default order time if still N/A
            if order_time == "N/A":
                order_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # Format phone number - handle N/A case properly
        if phone_number and phone_number != 'N/A':
            phoneformat62 = phone_number.replace(
                "62", "0") if phone_number.startswith("62") else phone_number
        else:
            phone_number = 'N/A'
            phoneformat62 = 'N/A'

        # Status emoji mapping
        status_emoji = {
            'PENDING': '🟡',
            'SUCCESS': '🟢',
            'CANCEL': '🔴',
            'REFUND': '🟠'
        }.get(order_status, '⚪')

        # Build updated message based on status
        if order_status in ['CANCEL', 'REFUND']:
            # For cancelled/refunded orders, show final status without action buttons
            cancel_reason = "Dibatalkan otomatis" if order_status == 'CANCEL' else "Refund diproses"
            message = f"📱 Status Order {service_name} {status_emoji}\n"
            message += f"🆔 Order ID: {order_id}\n"
            message += f"📞 Nomor: <code>{phone_number}</code> | <code>{phoneformat62}</code>\n"
            message += f"💵 Harga: {price}\n"
            message += f"📊 Status: {order_status}\n"
            message += f"🚫 {cancel_reason}\n"
            message += f"📧 SMS tidak tersedia (order dibatalkan)\n"
            message += f"🕒 Dipesan pada: {order_time}\n"
            message += f"🔄 Final Update: {datetime.now().strftime('%H:%M:%S')}"

            # No action buttons for cancelled/refunded orders
            reply_markup = None

        elif order_status == 'SUCCESS':
            # For successful orders, show final status with SMS and action buttons
            if sms_data:
                message = f"📱 SMS Order {service_name} {status_emoji}\n"
                message += f"🆔 Order ID: {order_id}\n"
                message += f"📞 Nomor: <code>{phone_number}</code> | <code>{phoneformat62}</code>\n"
                message += f"💵 Harga: {price}\n"
                message += f"📊 Status: {order_status}\n"
                message += f"📧 Total SMS: {len(sms_data)}\n"
                message += f"🕒 Dipesan pada: {order_time}\n"
                message += f"🔄 Final Update: {datetime.now().strftime('%H:%M:%S')}\n\n"

                for i, sms in enumerate(sms_data, 1):
                    sms_text = sms.get('sms', sms.get('fullSms', 'No text'))
                    full_sms = sms.get('fullSms', sms_text)

                    # Clean and format SMS text for better display
                    if sms_text:
                        sms_text = sms_text.strip()
                    if full_sms:
                        full_sms = full_sms.strip()

                    message += f"📧 SMS #{i}: <code>{sms_text}</code>\n"
                    if full_sms and full_sms != sms_text and len(full_sms) > len(sms_text):
                        message += f"📄 Full SMS: <code>{full_sms}</code>\n"
                    message += "\n"
            else:
                message = f"📱 Order {service_name} {status_emoji}\n"
                message += f"🆔 Order ID: {order_id}\n"
                message += f"📞 Nomor: <code>{phone_number}</code> | <code>{phoneformat62}</code>\n"
                message += f"💵 Harga: {price}\n"
                message += f"📊 Status: {order_status}\n"
                message += f"✅ Order berhasil diselesaikan\n"
                message += f"🕒 Dipesan pada: {order_time}\n"
                message += f"🔄 Final Update: {datetime.now().strftime('%H:%M:%S')}"

            # Show buttons for successful orders (no Cancel button)
            action_buttons = [
                [
                    InlineKeyboardButton(
                        "✅ Tandai Selesai", callback_data=f"finish_order_{order_id}")
                ],
                [
                    InlineKeyboardButton(
                        "🔄 Resend", callback_data=f"resend_order_{order_id}"),
                    InlineKeyboardButton(
                        "📱 Get SMS", callback_data=f"get_sms_{order_id}")
                ]
            ]

            if service_id != "N/A":
                action_buttons.append([
                    InlineKeyboardButton(
                        f"🔄 Order Again", callback_data=f"order_service_{service_id}")
                ])

            reply_markup = InlineKeyboardMarkup(action_buttons)

        else:
            # For pending orders, show normal interface
            if sms_data:
                message = f"📱 SMS Order {service_name} {status_emoji}\n"
                message += f"🆔 Order ID: {order_id}\n"
                message += f"📞 Nomor: <code>{phone_number}</code> | <code>{phoneformat62}</code>\n"
                message += f"💵 Harga: {price}\n"
                message += f"📊 Status: {order_status}\n"
                message += f"📧 Total SMS: {len(sms_data)}\n"
                message += f"🕒 Dipesan pada: {order_time}\n"
                message += f"🔄 Auto-Update: {datetime.now().strftime('%H:%M:%S')}\n\n"

                for i, sms in enumerate(sms_data, 1):
                    sms_text = sms.get('sms', sms.get('fullSms', 'No text'))
                    full_sms = sms.get('fullSms', sms_text)

                    # Clean and format SMS text for better display
                    if sms_text:
                        sms_text = sms_text.strip()
                    if full_sms:
                        full_sms = full_sms.strip()

                    message += f"📧 SMS #{i}: <code>{sms_text}</code>\n"
                    if full_sms and full_sms != sms_text and len(full_sms) > len(sms_text):
                        message += f"📄 Full: <code>{full_sms}</code>\n"
            else:
                message = f"📱 Status Order {service_name} {status_emoji}\n"
                message += f"🆔 Order ID: {order_id}\n"
                message += f"📞 Nomor: <code>{phone_number}</code> | <code>{phoneformat62}</code>\n"
                message += f"💵 Harga: {price}\n"
                message += f"📊 Status: {order_status}\n"
                message += f"📧 Menunggu SMS...\n"
                message += f"🕒 Dipesan pada: {order_time}\n"
                message += f"🔄 Auto-Update: {datetime.now().strftime('%H:%M:%S')}"

            # Show action buttons for pending orders
            action_buttons = [
                [
                    InlineKeyboardButton(
                        "❌ Batalkan OTP", callback_data=f"cancel_order_{order_id}"),
                    InlineKeyboardButton("✅ Tandai Selesai",
                                         callback_data=f"finish_order_{order_id}")
                ],
                [
                    InlineKeyboardButton(
                        "🔄 Resend", callback_data=f"resend_order_{order_id}"),
                    InlineKeyboardButton(
                        "📱 Get SMS", callback_data=f"get_sms_{order_id}")
                ]
            ]

            # Add Order Again button if service_id is available
            if service_id != "N/A":
                action_buttons.append([
                    InlineKeyboardButton(
                        f"🔄 Order Again", callback_data=f"order_service_{service_id}")
                ])

            reply_markup = InlineKeyboardMarkup(action_buttons)

        # Update the message
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=message,
            parse_mode="HTML",
            reply_markup=reply_markup
        )

    except Exception as e:
        logger.error(
            f"Failed to auto-update message for order {order_id}: {str(e)}")

# Enhanced async cancellation functions


async def schedule_delayed_cancellation(context: ContextTypes.DEFAULT_TYPE, order_id: str, delay_seconds: int = 120, message_id: int = None, chat_id: int = None):
    """
    Enhanced schedule an order to be cancelled after specified delay, but update UI immediately
    """
    async def cancel_after_delay():
        await asyncio.sleep(delay_seconds)

        # Cancel the order using enhanced async client
        url = f"{BASE_URL}order/{order_id}/1"
        headers = {"X-Api-Key": API_KEY}

        try:
            status_code, data = await http_client.patch(url, headers=headers)
            logger.info(
                f"Delayed-cancel request for order {order_id}: Status {status_code}, Response: {data}")

            if status_code == 200 and data:
                if data.get('status'):
                    logger.info(
                        f"Delayed-cancelled order {order_id} after {delay_seconds} seconds")

                    # Final cancellation message
                    if message_id and chat_id:
                        try:
                            final_message = (
                                f"❌ Pesanan dibatalkan!\n"
                                f"🆔 Order ID: {order_id}\n"
                                f"🚫 Alasan: Pembatalan manual\n"
                                f"🕒 Dibatalkan pada: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                                f"✅ Status: Berhasil dibatalkan"
                            )

                            await context.bot.edit_message_text(
                                chat_id=chat_id,
                                message_id=message_id,
                                text=final_message,
                                parse_mode="HTML"
                            )
                        except Exception as edit_error:
                            logger.error(
                                f"Failed to edit final cancel message for order {order_id}: {str(edit_error)}")
                else:
                    logger.error(
                        f"Failed to delayed-cancel order {order_id}: {data.get('message', 'Unknown error')}")
            else:
                logger.error(
                    f"HTTP Error {status_code} when delayed-cancelling order {order_id}")
        except Exception as e:
            logger.error(
                f"Exception during delayed-cancel of order {order_id}: {str(e)}")

        # Remove from user cancel requests
        if order_id in user_cancel_requests:
            del user_cancel_requests[order_id]

    # Store the cancellation task
    task = asyncio.create_task(cancel_after_delay())
    user_cancel_requests[order_id] = {
        'task': task,
        'message_id': message_id,
        'chat_id': chat_id
    }

# E-wallet service IDs that require account validation
EWALLET_SERVICES = {
    'dana': ['17'],  # Add DANA service IDs
    'ovo': ['53'],   # Add OVO service IDs
    'gopay': ['1208'],  # Add GoPay service IDs
    'linkaja': ['357']  # Add LinkAja service IDs
}

# Enhanced async e-wallet checker


async def cekrek(phone_number: str, service_type: str, max_retries: int = 3) -> Dict:
    """
    Enhanced async check if e-wallet account exists for given phone number with retries
    """
    try:
        # Format phone number (remove +62, keep leading 0)
        if phone_number.startswith('+62'):
            phone_number = '0' + phone_number[3:]
        elif phone_number.startswith('62'):
            phone_number = '0' + phone_number[2:]

        if service_type.lower() == 'dana' or service_type.lower() == '17':
            service_type = 'dana_active'

        # Mapping service_type to account_type value
        service_mapping = {
            'dana': 'kPTh+rsIRDKKTeYWkaPh10QxQ2tiTFFLTml5eFRBME1NbmxqVjFFSWRkL0crVEhxSERWK3V0YTdNbzA9',
            'dana_active': '3RMatI3XfkQj5iAAKnfvjnR0eUkzc1Y2Z25pcVhsTTNWU3hiWDB2ekVNRjI1TW9oRTFuVjRwT0tyMFk9',
            'shopeepay': '7BLFgSXukDhivFWpp9HAPXFMNzF4RTE3cGxkcVE0VmlZSTNUNEtkaWFWMXJnZU5kYVRKdnRoMFI1NjQ9',
            'linkaja_active': 'e/FsJuSdqID+MkmS4zCSNkU1dHJVc3hDQkgrVndnR3NNU1VVakJvVVk2TE9lbmZ4YS95WXZyWXZ4LzQ9',
            'ovo_active': 'sS/AWaTnhjm66U9P/vbjyUNhY2g3d3NxeXdLVk5ObVhTRElLRDdPUTNLYzB5ZTQycW13WFFxb2xNeUk9',
            'shopeepay_active': 'RRRj9w9nFnIvDwYu8vAcyHoxc2dMNnVaNkpLSGR3bE5pdkY2dytLQWJCZHlUdk90cUdiOUlKTGVNU2M9',
            'isaku_active': 'kGMDFe6LPSaVM8nmsrQCH0lGN0M2d1JFOGlDUTVDNlhoS2lkQ2hLYmsxV2k0NHFXbnc3RTYvOEcxdVk9',
            'gopay_customer_active': 'KgtVgI/JN0VrPz+qhwyU3UlIa1hsWHhMM0RGbHY5dkQyb3NvT0pGdUFudVFUcnltdzJlSE1iQW1XY2FqY2F2Z2dleVBuU2JZdVgyaGNoODA=',
            'gopay_driver_active': 'RlwUvZiBOdEYkEAV9A2+5WZ1cG1kL0I4b29sdWRFN1RBNUFtTjZjbXdGdWRJdWJyT3MzOXg4K1BROWdJeDN3RWVXWHJsQmhaZ3lqVm1UV0I='
        }

        account_type = service_mapping.get(service_type.lower())
        if not account_type:
            return {
                'status': 'error',
                'message': f"❌ Service type {service_type} tidak dikenal!",
                'account_name': 'N/A',
                'data': {}
            }

        url = "https://kedaimutasi.com/cekrekening/home/validate_account"
        payload = {
            'account_type': account_type,
            'account_number': phone_number
        }

        headers = {
            'Accept': "application/json, text/javascript, */*; q=0.01",
            'Accept-Language': "en-US,en;q=0.9,id;q=0.8",
            'DNT': "1",
            'Origin': "https://wisnucekrekening.xyz",
            'Referer': "https://wisnucekrekening.xyz/",
            'Sec-Fetch-Dest': "empty",
            'Sec-Fetch-Mode': "cors",
            'Sec-Fetch-Site': "cross-site",
            'user-agent': "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
        }

        retries = 0
        while retries < max_retries:
            try:
                status_code, data = await http_client.post(url, data=payload, headers=headers)

                if not data:
                    data = {}

                if status_code == 200:
                    if data.get('status') == 'success':
                        return {
                            'status': 'valid',
                            'message': f"✅ Nomor rekening valid untuk {service_type.upper()}!",
                            'account_name': data.get('account_name', 'N/A'),
                            'data': data
                        }
                    else:
                        return {
                            'status': 'invalid',
                            'message': f"❌ Nomor rekening tidak valid untuk {service_type.upper()}!",
                            'account_name': 'N/A',
                            'data': data
                        }
                elif status_code == 400:
                    retries += 1
                    if retries < max_retries:
                        await asyncio.sleep(1)  # Wait 1 second before retrying
                        continue
                    return {
                        'status': 'invalid',
                        'message': f"❌ Nomor rekening tidak valid untuk {service_type.upper()} setelah {max_retries} percobaan!",
                        'account_name': 'N/A',
                        'data': data
                    }
                else:
                    return {
                        'status': 'error',
                        'message': f"😕 Status tidak dikenal dari server ({status_code})",
                        'account_name': 'N/A',
                        'data': data
                    }

            except Exception as e:
                retries += 1
                if retries < max_retries:
                    await asyncio.sleep(1)  # Wait 1 second before retrying
                    continue
                return {
                    'status': 'error',
                    'message': f"❌ Terjadi kesalahan setelah {max_retries} percobaan: {str(e)}",
                    'account_name': 'N/A',
                    'data': {}
                }

        # This line should not be reached due to returns in the loop, but included for completeness
        return {
            'status': 'error',
            'message': f"❌ Gagal setelah {max_retries} percobaan",
            'account_name': 'N/A',
            'data': {}
        }

    except Exception as e:
        return {
            'status': 'error',
            'message': f"❌ Terjadi kesalahan: {str(e)}",
            'account_name': 'N/A',
            'data': {}
        }

# Function to get service type from service ID


def get_service_type(service_id: str) -> str:
    """Get e-wallet service type from service ID"""
    for service_type, ids in EWALLET_SERVICES.items():
        if service_id in ids:
            return service_type
    return None

# Enhanced async auto-cancellation


async def schedule_auto_cancellation(context: ContextTypes.DEFAULT_TYPE, order_id: str, message_id: int = None, chat_id: int = None):
    """
    Enhanced schedule an order to be cancelled after 10 minutes if no SMS is received
    """
    async def cancel_after_delay():
        await asyncio.sleep(600)  # 10 minutes = 600 seconds

        # Check if order still has no SMS
        url = f"{BASE_URL}order/status/{order_id}"
        headers = {"X-Api-Key": API_KEY}

        try:
            status_code, data = await http_client.get(url, headers)
            if status_code == 200 and data:
                if data.get('status') and data.get('data'):
                    order_data = data['data']
                    sms_data = order_data.get('Sms', [])
                    order_status = order_data.get('orderStatus', 'Unknown')

                    # Only cancel if no SMS received and order is still pending
                    if not sms_data and order_status == 'PENDING':
                        # Cancel the order using enhanced async client
                        cancel_url = f"{BASE_URL}order/{order_id}/1"
                        cancel_status, cancel_data = await http_client.patch(cancel_url, headers=headers)

                        logger.info(
                            f"Auto-cancel 10min request for order {order_id}: Status {cancel_status}")

                        if cancel_status == 200 and cancel_data:
                            if cancel_data.get('status'):
                                logger.info(
                                    f"Auto-cancelled order {order_id} after 10 minutes - no SMS received")

                                # Try to edit the original message to show auto-cancellation
                                if message_id and chat_id:
                                    try:
                                        cancelled_message = (
                                            f"❌ Pesanan dibatalkan otomatis!\n"
                                            f"🆔 Order ID: {order_id}\n"
                                            f"🚫 Alasan: Tidak ada SMS diterima dalam 10 menit\n"
                                            f"🕒 Dibatalkan pada: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                                            f"⏰ Auto-cancelled setelah 10 menit"
                                        )

                                        await context.bot.edit_message_text(
                                            chat_id=chat_id,
                                            message_id=message_id,
                                            text=cancelled_message,
                                            parse_mode="HTML"
                                        )
                                    except Exception as edit_error:
                                        logger.error(
                                            f"Failed to edit message for auto-cancelled order {order_id}: {str(edit_error)}")
                                        # Send new message as fallback
                                        try:
                                            await context.bot.send_message(
                                                chat_id=chat_id,
                                                text=f"❌ Order #{order_id} telah dibatalkan otomatis karena tidak ada SMS diterima dalam 10 menit.",
                                                parse_mode="HTML"
                                            )
                                        except Exception as send_error:
                                            logger.error(
                                                f"Failed to send auto-cancel notification for order {order_id}: {str(send_error)}")
                        else:
                            logger.error(
                                f"Failed to auto-cancel order {order_id}: {cancel_status}")
                    else:
                        logger.info(
                            f"Order {order_id} has SMS or is not pending, skipping auto-cancellation")
        except Exception as e:
            logger.error(
                f"Exception during auto-cancel check of order {order_id}: {str(e)}")

        # Remove from auto cancel timers
        if order_id in auto_cancel_timers:
            del auto_cancel_timers[order_id]

    # Store the cancellation task with message info
    task = asyncio.create_task(cancel_after_delay())
    auto_cancel_timers[order_id] = {
        'task': task,
        'message_id': message_id,
        'chat_id': chat_id
    }

# Enhanced async order cancellation


async def schedule_order_cancellation(context: ContextTypes.DEFAULT_TYPE, order_id: str, delay_seconds: int = 130, message_id: int = None, chat_id: int = None):
    """
    Enhanced schedule an order to be cancelled after specified delay (default 2 minutes 10 seconds)
    """
    async def cancel_after_delay():
        await asyncio.sleep(delay_seconds)

        # Cancel the order
        headers = {"X-Api-Key": API_KEY}
        url = f"{BASE_URL}order/{order_id}/1"

        try:
            status_code, data = await http_client.patch(url, headers=headers)
            logger.info(
                f"Cancel request for order {order_id}: Status {status_code}")

            if status_code == 200 and data:
                if data.get('status'):
                    logger.info(
                        f"Auto-cancelled order {order_id} after {delay_seconds} seconds")

                    # Try to edit the original message to show auto-cancellation
                    if message_id and chat_id:
                        try:
                            cancelled_message = (
                                f"❌ Pesanan dibatalkan otomatis!\n"
                                f"🆔 Order ID: {order_id}\n"
                                f"🚫 Alasan: Nomor sudah terdaftar pada e-wallet\n"
                                f"🕒 Dibatalkan pada: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                                f"⏰ Auto-cancelled setelah {delay_seconds} detik"
                            )

                            await context.bot.edit_message_text(
                                chat_id=chat_id,
                                message_id=message_id,
                                text=cancelled_message,
                                parse_mode="HTML"
                            )
                        except Exception as edit_error:
                            logger.error(
                                f"Failed to edit message for auto-cancelled order {order_id}: {str(edit_error)}")
                            # Send new message as fallback
                            try:
                                await context.bot.send_message(
                                    chat_id=chat_id,
                                    text=f"❌ Order #{order_id} telah dibatalkan otomatis karena nomor sudah terdaftar pada e-wallet.",
                                    parse_mode="HTML"
                                )
                            except Exception as send_error:
                                logger.error(
                                    f"Failed to send auto-cancel notification for order {order_id}: {str(send_error)}")
                else:
                    logger.error(
                        f"Failed to auto-cancel order {order_id}: {data.get('message', 'Unknown error')}")
            else:
                logger.error(
                    f"HTTP Error {status_code} when auto-cancelling order {order_id}")
        except Exception as e:
            logger.error(
                f"Exception during auto-cancel of order {order_id}: {str(e)}")

        # Remove from pending cancellations
        if order_id in pending_cancellations:
            del pending_cancellations[order_id]

    # Store the cancellation task with message info
    task = asyncio.create_task(cancel_after_delay())
    pending_cancellations[order_id] = {
        'task': task,
        'message_id': message_id,
        'chat_id': chat_id
    }

# Validate environment variables
if not API_KEY or not TELEGRAM_TOKEN or not AUTHORIZED_IDS:
    raise ValueError(
        "Please set SMSVIRTUAL_API_KEY, TELEGRAM_BOT_TOKEN, and AUTHORIZED_IDS in your .env file.")

# Setup colorful logging
logger = colorlog.getLogger(__name__)
logger.setLevel(logging.INFO)
console_handler = colorlog.StreamHandler(stream=sys.stdout)
console_handler.setFormatter(colorlog.ColoredFormatter(
    '%(log_color)s%(asctime)s - %(levelname)s - %(message)s',
    log_colors={
        'DEBUG': 'cyan', 'INFO': 'green', 'WARNING': 'yellow', 'ERROR': 'red', 'CRITICAL': 'red,bg_white',
    }
))
console_handler.stream = open(
    sys.stdout.fileno(), mode='w', encoding='utf-8', errors='replace')
file_handler = RotatingFileHandler("bot.log", maxBytes=10485760, backupCount=5)
file_handler.setFormatter(logging.Formatter(
    "%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(console_handler)
file_handler.setLevel(logging.INFO)
logger.addHandler(file_handler)


def log_user_id(user_id: str) -> None:
    try:
        if not os.path.exists(USER_ID_FILE):
            with open(USER_ID_FILE, "w", encoding='utf-8') as f:
                pass
        with open(USER_ID_FILE, "r", encoding='utf-8') as f:
            existing_ids = set(f.read().splitlines())
        if user_id not in existing_ids:
            with open(USER_ID_FILE, "a", encoding='utf-8') as f:
                f.write(f"{user_id}\n")
            logger.info(f"Logged new user ID: {user_id}")
    except Exception as e:
        logger.error(f"Error logging user ID {user_id}: {str(e)}")

# Enhanced functions untuk mengambil daftar user dengan pagination


def get_user_list(page=1, per_page=10):
    with open(".env", "r", encoding='utf-8') as f:
        for line in f:
            if line.startswith("AUTHORIZED_IDS="):
                ids = line.strip().split("=")[1].split(",")
                start = (page - 1) * per_page
                end = start + per_page
                return ids[start:end], len(ids)
    return [], 0


def get_service_list(page=1, per_page=10):
    try:
        with open("serviceotp.txt", "r", encoding='utf-8') as f:
            services = f.readlines()
            services = [line.strip().split(maxsplit=1)
                        for line in services if line.strip()]
            start = (page - 1) * per_page
            end = start + per_page
            return services[start:end], len(services)
    except FileNotFoundError:
        return [], 0


def delete_user_from_env(user_id):
    with open(".env", "r", encoding='utf-8') as f:
        lines = f.readlines()
    new_lines = []
    for line in lines:
        if line.startswith("AUTHORIZED_IDS="):
            ids = line.strip().split("=")[1].split(",")
            ids = [id for id in ids if id != user_id]
            new_line = f"AUTHORIZED_IDS={','.join(ids)}\n"
            new_lines.append(new_line)
        else:
            new_lines.append(line)
    with open(".env", "w", encoding='utf-8') as f:
        f.writelines(new_lines)
    if user_id in AUTHORIZED_IDS:
        AUTHORIZED_IDS.remove(user_id)


def delete_service_from_txt(service_id):
    try:
        with open("serviceotp.txt", "r", encoding='utf-8') as f:
            lines = f.readlines()
        with open("serviceotp.txt", "w", encoding='utf-8') as f:
            for line in lines:
                if not line.startswith(f"{service_id} "):
                    f.write(line)
    except FileNotFoundError:
        pass


async def show_list(query, context, type, page=1):
    if type == 'user':
        items, total = get_user_list(page)
        buttons = [[InlineKeyboardButton(
            f"User {id}", callback_data=f"delete_user_{id}")] for id in items]
    elif type == 'service':
        items, total = get_service_list(page)
        buttons = [[InlineKeyboardButton(
            f"{name} (ID: {id})", callback_data=f"delete_service_{id}")] for id, name in items]
    else:
        return

    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton(
            "Previous", callback_data=f"prev_{type}_{page-1}"))
    if page * 10 < total:
        nav_buttons.append(InlineKeyboardButton(
            "Next", callback_data=f"next_{type}_{page+1}"))
    if nav_buttons:
        buttons.append(nav_buttons)

    reply_markup = InlineKeyboardMarkup(buttons)
    message = await query.message.reply_text(f"List of {type}s (Page {page}):", reply_markup=reply_markup)
    context.user_data['list_message_id'] = message.message_id
    context.user_data['list_type'] = type
    context.user_data['list_page'] = page


async def check_authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = str(update.effective_user.id)
    log_user_id(user_id)
    if user_id not in AUTHORIZED_IDS:
        await update.message.reply_text("🚫 Oops! You don't have access yet. Please contact an admin to get started! 😊")
        logger.warning(f"Unauthorized access attempt by user ID: {user_id}")
        return False
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    log_user_id(user_id)
    keyboard = [
        [InlineKeyboardButton("💰 Check Balance", callback_data="balance")],
        [InlineKeyboardButton("📋 Available Services",
                              callback_data="services")],
        [InlineKeyboardButton("💸 Check Price", callback_data="check_price")],
        [InlineKeyboardButton("📱 Place Order", callback_data="order")],
        [InlineKeyboardButton(
            "📊 Active Orders", callback_data="active_orders")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    welcome_message = (
        f"🎉 Welcome to SMS Virtual Bot! 🎉\n\n"
        f"👋 Hello, <b>{update.effective_user.first_name}</b>!\n"
        f"🆔 Your ID: <code>{user_id}</code>\n\n"
        f"🚀 <b>Enhanced Features:</b>\n"
        f"• ⚡ Ultra-fast async processing\n"
        f"• 🔄 Real-time order monitoring\n"
        f"• 📱 Instant SMS delivery\n"
        f"• 💳 E-wallet validation\n"
        f"• 🌐 24/7 uptime support\n\n"
        f"Choose an option below to get started! 👇"
    )

    await update.message.reply_text(welcome_message, reply_markup=reply_markup, parse_mode="HTML")
    logger.info(f"User {user_id} started the bot")

# Enhanced async balance check


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_authorized(update, context):
        return

    user_id = str(update.effective_user.id)
    url = f"{BASE_URL}user/balance"
    headers = {"X-Api-Key": API_KEY}

    # Send initial "checking..." message
    message = await update.message.reply_text("⏳ Checking balance...")

    try:
        status_code, data = await http_client.get(url, headers)

        if status_code == 200 and data:
            if data.get('status'):
                balance_value = data.get('data', {}).get('balance', 'N/A')
                balance_message = (
                    f"💰 <b>Account Balance</b>\n\n"
                    f"💵 Balance: <code>${balance_value}</code>\n"
                    f"🕒 Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"✅ Status: Active"
                )
                await message.edit_text(balance_message, parse_mode="HTML")
            else:
                await message.edit_text(f"❌ Error: {data.get('message', 'Unknown error')}")
        else:
            await message.edit_text(f"❌ HTTP Error {status_code}")
    except Exception as e:
        await message.edit_text(f"❌ Exception: {str(e)}")

    logger.info(f"User {user_id} checked balance")

# Enhanced async services function


async def services(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    chat_id = update.message.chat_id
    if not await check_authorized(update, context):
        return

    # Send loading message
    loading_msg = await update.message.reply_text("⏳ Loading services...")

    url = f"{BASE_URL}services/"
    headers = {"X-Api-Key": API_KEY}

    try:
        status_code, data = await http_client.get(url, headers)

        if status_code == 200 and data:
            if data.get('status') and data.get('data'):
                services_list = data['data']
                content = f"SMSVirtual Services List\nGenerated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\nTotal Services: {len(services_list)}\n{'=' * 50}\n\n"

                for i, service in enumerate(services_list, 1):
                    service_id = service.get('id', 'N/A')
                    service_name = service.get('serviceName', 'Unknown')
                    content += f"{i:3}. ID: {service_id:4} | {service_name}\n"

                file_buffer = io.BytesIO()
                file_buffer.write(content.encode('utf-8'))
                file_buffer.seek(0)

                await context.bot.send_document(
                    chat_id=chat_id,
                    document=file_buffer,
                    filename=f"services_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
                    caption="📄 Services List - Download completed!"
                )

                await loading_msg.edit_text(f"⚙️ Services data sent as file!\n📊 Total: {len(services_list)} services")
            else:
                await loading_msg.edit_text(f"⚠️ Error: {data.get('message', 'No services data available')}")
        else:
            await loading_msg.edit_text(f"❌ HTTP Error {status_code}")
    except Exception as e:
        await loading_msg.edit_text(f"❌ Error: {str(e)}")

    logger.info(f"User {user_id} downloaded services list")


async def services_callback(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(query.from_user.id)
    chat_id = query.message.chat_id

    # Send loading message
    loading_msg = await query.message.reply_text("⏳ Loading services...")

    url = f"{BASE_URL}services/"
    headers = {"X-Api-Key": API_KEY}

    try:
        status_code, data = await http_client.get(url, headers)

        if status_code == 200 and data:
            if data.get('status') and data.get('data'):
                services_list = data['data']
                content = f"SMSVirtual Services List\nGenerated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\nTotal Services: {len(services_list)}\n{'=' * 50}\n\n"

                for i, service in enumerate(services_list, 1):
                    service_id = service.get('id', 'N/A')
                    service_name = service.get('serviceName', 'Unknown')
                    content += f"{i:3}. ID: {service_id:4} | {service_name}\n"

                file_buffer = io.BytesIO()
                file_buffer.write(content.encode('utf-8'))
                file_buffer.seek(0)

                await context.bot.send_document(
                    chat_id=chat_id,
                    document=file_buffer,
                    filename=f"services_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
                    caption="📄 Services List - Download completed!"
                )

                await loading_msg.edit_text(f"⚙️ Services data sent as file!\n📊 Total: {len(services_list)} services")
            else:
                await loading_msg.edit_text(f"⚠️ Error: {data.get('message', 'No services data available')}")
        else:
            await loading_msg.edit_text(f"❌ HTTP Error {status_code}")
    except Exception as e:
        await loading_msg.edit_text(f"❌ Error: {str(e)}")

    logger.info(f"User {user_id} downloaded services list via callback")

# Enhanced async order functions


async def order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    if not await check_authorized(update, context):
        return

    try:
        with open("serviceotp.txt", "r", encoding='utf-8') as f:
            services = f.readlines()
    except FileNotFoundError:
        await update.message.reply_text("❌ File serviceotp.txt tidak ditemukan!")
        logger.error(
            f"User {user_id} attempted /order but serviceotp.txt is missing")
        return

    service_buttons = []
    for line in services:
        parts = line.strip().split(maxsplit=1)
        if len(parts) == 2:
            service_id, service_name = parts
            button = InlineKeyboardButton(
                service_name, callback_data=f"order_service_{service_id}")
            service_buttons.append([button])

    if service_buttons:
        reply_markup = InlineKeyboardMarkup(service_buttons)
        await update.message.reply_text("📞 Pilih layanan yang ingin dipesan:", reply_markup=reply_markup)
    else:
        await update.message.reply_text("❌ Tidak ada layanan yang tersedia!")

    logger.info(f"User {user_id} executed /order command")


async def order_callback(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(query.from_user.id)
    if user_id not in AUTHORIZED_IDS:
        await query.message.reply_text("🚫 Akses ditolak! Silakan hubungi admin untuk mendapatkan akses. 😊")
        logger.warning(f"Unauthorized access attempt by user ID: {user_id}")
        return

    try:
        with open("serviceotp.txt", "r", encoding='utf-8') as f:
            services = f.readlines()
    except FileNotFoundError:
        await query.message.reply_text("❌ File serviceotp.txt tidak ditemukan!")
        logger.error(
            f"User {user_id} attempted order but serviceotp.txt is missing")
        return

    service_buttons = []
    for line in services:
        parts = line.strip().split(maxsplit=1)
        if len(parts) == 2:
            service_id, service_name = parts
            button = InlineKeyboardButton(
                service_name, callback_data=f"order_service_{service_id}")
            service_buttons.append([button])

    if service_buttons:
        reply_markup = InlineKeyboardMarkup(service_buttons)
        await query.message.reply_text("📞 Pilih layanan yang ingin dipesan:", reply_markup=reply_markup)
    else:
        await query.message.reply_text("❌ Tidak ada layanan yang tersedia!")

    logger.info(f"User {user_id} clicked Order Service button")

# Enhanced async place_order function


async def place_order(query, context: ContextTypes.DEFAULT_TYPE, service_id: str) -> None:
    user_id = str(query.from_user.id)

    # Get service name from serviceotp.txt
    service_name = "Unknown Service"
    try:
        with open("serviceotp.txt", "r", encoding='utf-8') as f:
            for line in f:
                parts = line.strip().split(maxsplit=1)
                if len(parts) == 2 and parts[0] == service_id:
                    service_name = parts[1]
                    break
    except FileNotFoundError:
        pass

    url_price = f"{BASE_URL}price/{service_id}/"
    headers = {"X-Api-Key": API_KEY}

    try:
        # Enhanced async price retrieval
        status_code, data = await http_client.get(url_price, headers)

        if status_code != 200:
            await query.message.reply_text(f"❌ Gagal mengambil harga: HTTP Error {status_code}")
            return

        if not (data and data.get('status') and data.get('data')):
            await query.message.reply_text("❌ Data harga tidak tersedia untuk layanan ini!")
            return

        price_data = data['data']
        country_data = next(
            (item for item in price_data if item.get('country') == 7), None)

        if not country_data:
            await query.message.reply_text("❌ Data harga untuk negara ID 7 tidak ditemukan!")
            return

        # Prepare pricing for order attempts
        standard_price = country_data.get('priceUsd', 0)
        custom_prices = country_data.get('customPrice', [])

        if custom_prices:
            prices_to_try = sorted(
                [cp.get('price', cp.get('amount', standard_price)) for cp in custom_prices])
        else:
            prices_to_try = [standard_price]

        max_attempts = min(3, len(prices_to_try))
        min_price = min(prices_to_try)
        max_price = max(prices_to_try)

        # Start order attempts
        for attempt in range(max_attempts):
            custom_price = prices_to_try[attempt]
            payload = {
                "country": 7,
                "service": int(service_id),
                "operator": "",
                "customPrice": custom_price + 0.00002,
                "rangePrice": {"min": min_price, "max": max_price}
            }

            order_url = "https://api.smsvirtual.co/v1/order/"
            headers_order = {**headers, "Content-Type": "application/json"}

            # Use enhanced async client for order
            status_code, order_data = await http_client.post(order_url, json_data=payload, headers=headers_order)

            if status_code == 200 and order_data:
                if order_data.get('status'):
                    # Order successful
                    order_id = order_data['data'].get('id', 'N/A')
                    number = order_data['data'].get('phone', 'N/A')
                    phoneformat62 = number.replace(
                        "62", "0") if number.startswith("62") else number
                    price = order_data['data'].get('price', 'N/A')

                    # Check if the service requires account validation
                    service_type = get_service_type(service_id)
                    status_text = ""

                    if service_type:
                        validation_result = await cekrek(phoneformat62, service_type)
                        if validation_result['status'] == 'valid':
                            status_text = " | ✅ Terdaftar"
                            await schedule_order_cancellation(context, order_id)
                        elif validation_result['status'] == 'invalid':
                            status_text = " | ❌ Tidak Terdaftar"
                        else:
                            status_text = " | ❓ Status Tidak Diketahui"

                    message = (
                        f"✅ Pesanan berhasil {service_name}!\n"
                        f"🆔 Order ID: {order_id}{status_text}\n"
                        f"📞 Nomor: <code>{number}</code> | <code>{phoneformat62}</code>\n"
                        f"💵 Harga: ${price:.5f}\n"
                        f"🕒 Dipesan pada: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    )

                    # Add action buttons for successful order
                    action_buttons = [
                        [
                            InlineKeyboardButton(
                                "❌ Batalkan OTP", callback_data=f"cancel_order_{order_id}"),
                            InlineKeyboardButton(
                                "✅ Tandai Selesai", callback_data=f"finish_order_{order_id}")
                        ],
                        [
                            InlineKeyboardButton(
                                "🔄 Resend", callback_data=f"resend_order_{order_id}"),
                            InlineKeyboardButton(
                                "📱 Get SMS", callback_data=f"get_sms_{order_id}")
                        ],
                        [
                            InlineKeyboardButton(
                                f"🔄 Order Again", callback_data=f"order_service_{service_id}")
                        ]
                    ]
                    reply_markup = InlineKeyboardMarkup(action_buttons)

                    sent_message = await query.message.reply_text(message, parse_mode="HTML", reply_markup=reply_markup)

                    # Schedule appropriate cancellation/monitoring
                    if service_type and status_text == " | ✅ Terdaftar":
                        await schedule_order_cancellation(
                            context, order_id, delay_seconds=130,
                            message_id=sent_message.message_id, chat_id=sent_message.chat_id
                        )
                    else:
                        await schedule_auto_cancellation(
                            context, order_id,
                            message_id=sent_message.message_id, chat_id=sent_message.chat_id
                        )
                        await monitor_order_sms(
                            context, order_id, sent_message.message_id, sent_message.chat_id, initial_sms_count=0
                        )

                    # Log order
                    try:
                        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        log_entry = f"{timestamp},{user_id},{order_id},{service_id},{service_name},{number},{price:.5f},ORDERED,\n"
                        with open("logorder.txt", "a", encoding='utf-8') as f:
                            f.write(log_entry)
                    except Exception as e:
                        logger.error(
                            f"Failed to log order {order_id}: {str(e)}")

                    # Store order information
                    store_order_info(order_id, service_id,
                                     service_name, number, price, user_id)
                    logger.info(
                        f"User {user_id} placed an order for service {service_id} on attempt {attempt+1}")
                    return
                else:
                    # If failed, check if it's due to "no number"
                    error_message = order_data.get(
                        'message', 'Kesalahan tidak diketahui')
                    if "no number" not in error_message.lower():
                        await query.message.reply_text(f"⚠️ Gagal: {error_message}")
                        return
            else:
                await query.message.reply_text(f"❌ HTTP Error {status_code}")
                return

        # If all attempts failed
        await query.message.reply_text("❌ Gagal memesan nomor setelah 3 percobaan. Tidak ada nomor tersedia.")

    except Exception as e:
        await query.message.reply_text(f"❌ Terjadi kesalahan: {str(e)}")
        logger.error(f"Error placing order for user {user_id}: {str(e)}")

# Enhanced button callback handler


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    log_user_id(user_id)

    if user_id not in AUTHORIZED_IDS:
        await query.message.reply_text("🚫 Access denied! Please contact an admin to gain access. 😊")
        logger.warning(
            f"Unauthorized button access attempt by user ID: {user_id}")
        return

    # Handle all callback data efficiently
    if query.data == "balance":
        await balance_callback(query, context)
    elif query.data == "services":
        await services_callback(query, context)
    elif query.data == "cekprice":
        waiting_for_service_id[user_id] = True
        await query.message.reply_text("🔍 Please send the service ID to check its price (e.g., 399):")
    elif query.data == "order":
        await order_callback(query, context)
    elif query.data == "active_orders":
        await active_orders_callback(query, context)
    elif query.data.startswith("order_service_"):
        service_id = query.data.split("_")[2]
        await place_order(query, context, service_id)
    elif query.data == "admin_tools" and user_id in ADMIN_IDS:
        admin_keyboard = [
            [InlineKeyboardButton("🔐 Add User", callback_data="add_user")],
            [InlineKeyboardButton(
                "🗑️ Delete User", callback_data="delete_user")],
            [InlineKeyboardButton(
                "➕ Add Service", callback_data="add_service")],
            [InlineKeyboardButton("➖ Delete Service",
                                  callback_data="delete_service")]
        ]
        reply_markup = InlineKeyboardMarkup(admin_keyboard)
        await query.message.reply_text("🔧 Admin Tools:", reply_markup=reply_markup)
    # ... other callback handlers would continue here


async def balance_callback(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(query.from_user.id)

    # Send loading message
    loading_msg = await query.message.reply_text("⏳ Checking balance...")

    url = f"{BASE_URL}profile/"
    headers = {"X-Api-Key": API_KEY}

    try:
        status_code, data = await http_client.get(url, headers)

        if status_code == 200 and data:
            if data.get('status'):
                balance_value = data['data']['balance']
                email = data['data']['email']
                message = (
                    f"💰 <b>Account Balance</b>\n\n"
                    f"📧 Email: <code>{email}</code>\n"
                    f"💸 Balance: <code>${balance_value:.4f}</code>\n"
                    f"🕒 Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                await loading_msg.edit_text(message, parse_mode="HTML")
            else:
                await loading_msg.edit_text(f"⚠️ Error: {data.get('message', 'Unknown error')}")
        else:
            await loading_msg.edit_text(f"❌ HTTP Error {status_code}")
    except Exception as e:
        await loading_msg.edit_text(f"❌ Error: {str(e)}")

    logger.info(f"User {user_id} checked balance via callback")

# Enhanced async check price


async def check_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    if not await check_authorized(update, context):
        return

    if not context.args:
        await update.message.reply_text("🔍 Please provide a service ID: /cekprice <service_id>\nExample: /cekprice 399")
        return

    service_id = context.args[0]
    if not service_id.isdigit():
        await update.message.reply_text("❌ Please provide a valid numeric service ID!")
        return

    # Send loading message
    loading_msg = await update.message.reply_text("⏳ Checking price...")

    url = f"{BASE_URL}price/{service_id}/"
    headers = {"X-Api-Key": API_KEY}

    try:
        status_code, data = await http_client.get(url, headers)

        if status_code == 200 and data:
            if data.get('status') and data.get('data'):
                price_data = data['data']
                message = f"💰 **Price Information for Service ID: {service_id}**\n\n"

                for item in price_data:
                    country_name = item.get('countryName', 'Unknown')
                    country_id = item.get('country', 'N/A')
                    available = item.get('available', 0)
                    price_usd = item.get('priceUsd', 0)
                    custom_prices = item.get('customPrice', [])

                    message += f"🌍 **{country_name}** (ID: {country_id})\n"
                    message += f"📱 Available: {available:,} numbers\n"
                    message += f"💵 Standard Price: ${price_usd:.5f} USD\n"

                    if custom_prices:
                        message += f"🎯 **Custom Prices:**\n"
                        for i, custom_price in enumerate(custom_prices[:5], 1):
                            if isinstance(custom_price, dict):
                                custom_amount = custom_price.get(
                                    'price', custom_price.get('amount', 'N/A'))
                                custom_type = custom_price.get(
                                    'type', custom_price.get('name', f'Option {i}'))
                                message += f"   • {custom_type}: ${custom_amount:.5f} USD\n"
                            else:
                                message += f"   • Custom {i}: ${custom_price:.5f} USD\n"
                    else:
                        message += f"🎯 Custom Prices: Not available\n"

                    message += f"\n" + "─" * 30 + "\n"

                message += f"🕒 Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                await loading_msg.edit_text(message, parse_mode="Markdown")
            else:
                await loading_msg.edit_text(f"⚠️ Error: {data.get('message', 'No price data available')}")
        else:
            await loading_msg.edit_text(f"❌ HTTP Error {status_code}")
    except Exception as e:
        await loading_msg.edit_text(f"❌ Error: {str(e)}")

    logger.info(f"User {user_id} checked price for service ID: {service_id}")

# Enhanced async active orders


async def active_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    if not await check_authorized(update, context):
        return

    # Send loading message
    loading_msg = await update.message.reply_text("⏳ Loading active orders...")

    url = f"{BASE_URL}order/active"
    headers = {"X-Api-Key": API_KEY}

    try:
        status_code, data = await http_client.get(url, headers)

        if status_code == 200 and data:
            if data.get('status') and data.get('data'):
                orders = data['data']
                if not orders:
                    await loading_msg.edit_text("📋 Tidak ada pesanan aktif saat ini.")
                    return

                message = f"📋 **Pesanan Aktif** ({len(orders)} pesanan)\n\n"

                for i, order in enumerate(orders, 1):
                    order_id = order.get('orderId', 'N/A')
                    number = order.get('number', 'N/A')
                    phoneformat62 = number.replace(
                        "62", "0") if number.startswith("62") else number
                    operator = order.get('operator', 'Unknown')
                    price = order.get('price', 0)
                    status = order.get('orderStatus', 'Unknown')
                    service_id = order.get('serviceId', 'N/A')
                    country_id = order.get('countryId', 'N/A')
                    expired_at = order.get('expiredAt', 0)
                    sms_count = len(order.get('Sms', []))

                    if expired_at:
                        try:
                            expired_date = datetime.fromtimestamp(
                                expired_at / 1000).strftime('%Y-%m-%d %H:%M:%S')
                        except:
                            expired_date = 'Unknown'
                    else:
                        expired_date = 'Unknown'

                    status_emoji = "🟡" if status == "PENDING" else "🟢" if status == "COMPLETED" else "🔴"

                    message += f"{i}. {status_emoji} **Order #{order_id}**\n"
                    message += f"📞 Nomor: `{number}` | `{phoneformat62}`\n"
                    message += f"📡 Operator: {operator}\n"
                    message += f"🆔 Service ID: {service_id} | Country: {country_id}\n"
                    message += f"💵 Harga: ${price:.5f}\n"
                    message += f"📊 Status: {status}\n"
                    message += f"📧 SMS Diterima: {sms_count}\n"
                    message += f"⏰ Expired: {expired_date}\n"
                    message += "─" * 30 + "\n"

                await loading_msg.edit_text(message, parse_mode="Markdown")
            else:
                await loading_msg.edit_text(f"⚠️ Error: {data.get('message', 'No active orders found')}")
        else:
            await loading_msg.edit_text(f"❌ HTTP Error {status_code}")
    except Exception as e:
        await loading_msg.edit_text(f"❌ Error: {str(e)}")

    logger.info(f"User {user_id} checked active orders")


async def active_orders_callback(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(query.from_user.id)

    # Send loading message
    loading_msg = await query.message.reply_text("⏳ Loading active orders...")

    url = f"{BASE_URL}order/active"
    headers = {"X-Api-Key": API_KEY}

    try:
        status_code, data = await http_client.get(url, headers)

        if status_code == 200 and data:
            if data.get('status') and data.get('data'):
                orders = data['data']
                if not orders:
                    await loading_msg.edit_text("📋 Tidak ada pesanan aktif saat ini.")
                    return

                message = f"📋 **Pesanan Aktif** ({len(orders)} pesanan)\n\n"

                for i, order in enumerate(orders, 1):
                    order_id = order.get('orderId', 'N/A')
                    number = order.get('number', 'N/A')
                    phoneformat62 = number.replace(
                        "62", "0") if number.startswith("62") else number
                    operator = order.get('operator', 'Unknown')
                    price = order.get('price', 0)
                    status = order.get('orderStatus', 'Unknown')
                    service_id = order.get('serviceId', 'N/A')
                    sms_count = len(order.get('Sms', []))

                    status_emoji = "🟡" if status == "PENDING" else "🟢" if status == "COMPLETED" else "🔴"

                    message += f"{i}. {status_emoji} **Order #{order_id}**\n"
                    message += f"📞 Nomor: `{number}` | `{phoneformat62}`\n"
                    message += f"📡 Operator: {operator}\n"
                    message += f"🆔 Service ID: {service_id}\n"
                    message += f"💵 Harga: ${price:.5f}\n"
                    message += f"📊 Status: {status}\n"
                    message += f"📧 SMS: {sms_count}\n"
                    message += "─" * 20 + "\n"

                await loading_msg.edit_text(message, parse_mode="Markdown")
            else:
                await loading_msg.edit_text(f"⚠️ Error: {data.get('message', 'No active orders found')}")
        else:
            await loading_msg.edit_text(f"❌ HTTP Error {status_code}")
    except Exception as e:
        await loading_msg.edit_text(f"❌ Error: {str(e)}")

    logger.info(f"User {user_id} checked active orders via callback")

# Enhanced text message handler


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)

    if user_id in waiting_for_user_id:
        if waiting_for_user_id[user_id] == 'add':
            new_user_id = update.message.text.strip()
            if not new_user_id.isdigit():
                await update.message.reply_text("❌ Please provide a valid numeric User ID!")
                return

            try:
                with open(".env", "r", encoding='utf-8') as f:
                    lines = f.readlines()

                new_lines = []
                for line in lines:
                    if line.startswith("AUTHORIZED_IDS="):
                        ids = line.strip().split("=")[1].split(",")
                        if new_user_id in ids:
                            await update.message.reply_text(f"❌ User {new_user_id} is already authorized!")
                            return
                        ids.append(new_user_id)
                        new_line = f"AUTHORIZED_IDS={','.join(ids)}\n"
                        new_lines.append(new_line)
                    else:
                        new_lines.append(line)

                with open(".env", "w", encoding='utf-8') as f:
                    f.writelines(new_lines)

                AUTHORIZED_IDS.add(new_user_id)
                await update.message.reply_text(f"✅ Success! User {new_user_id} has been added! 🎉")
                logger.info(f"Admin {user_id} added user {new_user_id}")
            except Exception as e:
                await update.message.reply_text(f"❌ Error adding user: {str(e)}")
                logger.error(f"Error adding user {new_user_id}: {str(e)}")

            del waiting_for_user_id[user_id]

    elif user_id in waiting_for_service_input:
        if waiting_for_service_input[user_id] == 'add':
            input_text = update.message.text.strip()
            parts = input_text.split(maxsplit=1)
            if len(parts) != 2 or not parts[0].isdigit():
                await update.message.reply_text("❌ Format salah! Gunakan: <service_id> <service_name>\nContoh: 123 WhatsApp")
                return

            service_id, service_name = parts
            try:
                # Check if service already exists
                existing_services = []
                try:
                    with open("serviceotp.txt", "r", encoding='utf-8') as f:
                        existing_services = f.readlines()
                except FileNotFoundError:
                    pass

                # Check for duplicate service ID
                for line in existing_services:
                    if line.strip().startswith(f"{service_id} "):
                        await update.message.reply_text(f"❌ Service ID {service_id} sudah ada!")
                        return

                # Add new service
                with open("serviceotp.txt", "a", encoding='utf-8') as f:
                    f.write(f"{service_id} {service_name}\n")
                    f.flush()

                await update.message.reply_text(f"✅ Service berhasil ditambahkan!\n🆔 ID: {service_id}\n📱 Nama: {service_name}")
                logger.info(
                    f"Admin {user_id} added service {service_id}: {service_name}")

            except Exception as e:
                await update.message.reply_text(f"❌ Error: {str(e)}")
                logger.error(f"Error adding service: {str(e)}")

            del waiting_for_service_input[user_id]

    elif user_id in waiting_for_service_id and waiting_for_service_id[user_id]:
        service_id = update.message.text.strip()
        if not service_id.isdigit():
            await update.message.reply_text("❌ Please provide a valid numeric service ID!")
            return

        # Send loading message
        loading_msg = await update.message.reply_text("⏳ Checking price...")

        url = f"{BASE_URL}price/{service_id}/"
        headers = {"X-Api-Key": API_KEY}

        try:
            status_code, data = await http_client.get(url, headers)

            if status_code == 200 and data:
                if data.get('status') and data.get('data'):
                    price_data = data['data']
                    message = f"💰 **Price for Service ID: {service_id}**\n\n"

                    for item in price_data:
                        country_name = item.get('countryName', 'Unknown')
                        country_id = item.get('country', 'N/A')
                        available = item.get('available', 0)
                        price_usd = item.get('priceUsd', 0)
                        custom_prices = item.get('customPrice', [])

                        message += f"🌍 **{country_name}** (ID: {country_id})\n"
                        message += f"📱 Available: {available:,} numbers\n"
                        message += f"💵 Standard: ${price_usd:.5f} USD\n"

                        if custom_prices:
                            message += f"🎯 **Custom Prices:**\n"
                            for i, cp in enumerate(custom_prices[:3], 1):
                                if isinstance(cp, dict):
                                    amount = cp.get(
                                        'price', cp.get('amount', 'N/A'))
                                    name = cp.get('type', cp.get(
                                        'name', f'Option {i}'))
                                    message += f"   • {name}: ${amount:.5f}\n"
                                else:
                                    message += f"   • Option {i}: ${cp:.5f}\n"

                        message += "─" * 25 + "\n"

                    message += f"🕒 Updated: {datetime.now().strftime('%H:%M:%S')}"
                    await loading_msg.edit_text(message, parse_mode="Markdown")
                else:
                    await loading_msg.edit_text(f"⚠️ Error: {data.get('message', 'No price data')}")
            else:
                await loading_msg.edit_text(f"❌ HTTP Error {status_code}")
        except Exception as e:
            await loading_msg.edit_text(f"❌ Error: {str(e)}")

        waiting_for_service_id[user_id] = False
        logger.info(f"User {user_id} checked price for service {service_id}")


async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    if not await check_authorized(update, context):
        return

    if user_id not in ADMIN_IDS:
        await update.message.reply_text("🚫 Sorry, only admins can add new users! 😊")
        logger.warning(f"Non-admin user {user_id} attempted to add user")
        return

    if not context.args:
        await update.message.reply_text("🔐 Please provide a user ID: /adduser <user_id>")
        return

    new_user_id = context.args[0]
    try:
        with open(".env", "r", encoding='utf-8') as f:
            lines = f.readlines()

        new_lines = []
        for line in lines:
            if line.startswith("AUTHORIZED_IDS="):
                ids = line.strip().split("=")[1].split(",")
                if new_user_id in ids:
                    await update.message.reply_text(f"❌ User {new_user_id} is already authorized!")
                    return
                ids.append(new_user_id)
                new_line = f"AUTHORIZED_IDS={','.join(ids)}\n"
                new_lines.append(new_line)
            else:
                new_lines.append(line)

        with open(".env", "w", encoding='utf-8') as f:
            f.writelines(new_lines)

        AUTHORIZED_IDS.add(new_user_id)
        await update.message.reply_text(f"✅ Success! User {new_user_id} has been added! 🎉")
        logger.info(f"Admin {user_id} added user {new_user_id}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")
        logger.error(f"Error adding user {new_user_id}: {str(e)}")


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin tools command"""
    user_id = str(update.effective_user.id)
    if not await check_authorized(update, context):
        return

    if user_id not in ADMIN_IDS:
        await update.message.reply_text("🚫 Sorry, only admins can access these tools! 😊")
        logger.warning(
            f"Non-admin user {user_id} attempted to access admin tools")
        return

    admin_keyboard = [
        [InlineKeyboardButton("🔐 Add User", callback_data="add_user")],
        [InlineKeyboardButton("🗑️ Delete User", callback_data="delete_user")],
        [InlineKeyboardButton("➕ Add Service", callback_data="add_service")],
        [InlineKeyboardButton("➖ Delete Service",
                              callback_data="delete_service")]
    ]
    reply_markup = InlineKeyboardMarkup(admin_keyboard)
    await update.message.reply_text("🔧 Admin Tools:", reply_markup=reply_markup)
    logger.info(f"Admin {user_id} accessed admin tools")

# Enhanced async SMS and order management functions


async def get_sms(query, context: ContextTypes.DEFAULT_TYPE, order_id: str) -> None:
    """Enhanced async function to get SMS for order"""
    user_id = str(query.from_user.id)
    url = f"{BASE_URL}order/status/{order_id}"
    headers = {"X-Api-Key": API_KEY}

    try:
        status_code, data = await http_client.get(url, headers)

        if status_code == 200 and data:
            if data.get('status') and data.get('data'):
                order_data = data['data']
                order_status = order_data.get('orderStatus', 'Unknown')
                sms_data = order_data.get('Sms', [])

                # Status emoji mapping
                status_emoji = {
                    'PENDING': '🟡',
                    'SUCCESS': '🟢',
                    'CANCEL': '🔴',
                    'REFUND': '🟠'
                }.get(order_status, '⚪')

                # Get order info from storage first
                stored_order = get_order_info(order_id)
                service_name = stored_order['service_name']
                service_id = stored_order['service_id']
                order_time = stored_order['order_time']
                stored_price = stored_order['price']

                # Get phone number from API response or storage
                phone_number = order_data.get(
                    'number', stored_order['phone_number'])
                phoneformat62 = phone_number.replace(
                    "62", "0") if phone_number.startswith("62") else phone_number

                # Use stored price if available, otherwise API price
                if stored_price != "N/A":
                    price = f"${float(stored_price):.5f}" if isinstance(
                        stored_price, (int, float)) else stored_price
                else:
                    api_price = order_data.get('price', 'N/A')
                    price = f"${float(api_price):.5f}" if api_price != 'N/A' and isinstance(
                        api_price, (int, float)) else api_price

                # Extract from message as fallback
                if service_name == "Unknown Service" or phone_number == "N/A":
                    original_message = query.message
                    if original_message and original_message.text:
                        service_info = extract_service_info_from_message(
                            original_message.text)
                        if service_name == "Unknown Service":
                            service_name = service_info['service_name']
                        if phone_number == "N/A":
                            phone_number = service_info['phone_number']
                        if order_time == "N/A":
                            order_time = service_info['order_time']
                        if price in ["N/A"] and service_info['price'] != "N/A":
                            price = service_info['price']

                # Build message based on status
                if order_status in ['CANCEL', 'REFUND']:
                    cancel_reason = "Dibatalkan otomatis" if order_status == 'CANCEL' else "Refund diproses"
                    message = f"📱 Order {service_name} {status_emoji}\n"
                    message += f"🆔 Order ID: {order_id}\n"
                    message += f"📞 Nomor: <code>{phone_number}</code> | <code>{phoneformat62}</code>\n"
                    message += f"💵 Harga: {price}\n"
                    message += f"📊 Status: {order_status}\n"
                    message += f"🚫 {cancel_reason}\n"
                    message += f"🕒 Dipesan pada: {order_time}\n"
                    message += f"🔄 Final Update: {datetime.now().strftime('%H:%M:%S')}"
                    reply_markup = None

                elif order_status == 'SUCCESS':
                    if sms_data:
                        message = f"📱 SMS Order {service_name} {status_emoji}\n"
                        message += f"🆔 Order ID: {order_id}\n"
                        message += f"📞 Nomor: <code>{phone_number}</code> | <code>{phoneformat62}</code>\n"
                        message += f"💵 Harga: {price}\n"
                        message += f"📊 Status: {order_status}\n"
                        message += f"📧 Total SMS: {len(sms_data)}\n"
                        message += f"🕒 Dipesan pada: {order_time}\n\n"

                        for i, sms in enumerate(sms_data, 1):
                            sms_text = sms.get(
                                'sms', sms.get('fullSms', 'No text'))
                            full_sms = sms.get('fullSms', sms_text)

                            if sms_text:
                                sms_text = sms_text.strip()
                            if full_sms:
                                full_sms = full_sms.strip()

                            message += f"📧 SMS #{i}: <code>{sms_text}</code>\n"
                            if full_sms and full_sms != sms_text and len(full_sms) > len(sms_text):
                                message += f"📄 Full SMS: <code>{full_sms}</code>\n"
                            message += "\n"
                    else:
                        message = f"📱 Order {service_name} {status_emoji}\n"
                        message += f"🆔 Order ID: {order_id}\n"
                        message += f"📞 Nomor: <code>{phone_number}</code> | <code>{phoneformat62}</code>\n"
                        message += f"💵 Harga: {price}\n"
                        message += f"📊 Status: {order_status}\n"
                        message += f"✅ Order berhasil diselesaikan\n"
                        message += f"🕒 Dipesan pada: {order_time}"

                    # Success order buttons
                    action_buttons = [
                        [InlineKeyboardButton(
                            "✅ Tandai Selesai", callback_data=f"finish_order_{order_id}")],
                        [
                            InlineKeyboardButton(
                                "🔄 Resend", callback_data=f"resend_order_{order_id}"),
                            InlineKeyboardButton(
                                "📱 Get SMS", callback_data=f"get_sms_{order_id}")
                        ]
                    ]
                    if service_id != "N/A":
                        action_buttons.append([InlineKeyboardButton(
                            f"🔄 Order Again", callback_data=f"order_service_{service_id}")])
                    reply_markup = InlineKeyboardMarkup(action_buttons)

                else:
                    # Pending orders
                    if not sms_data:
                        message = f"📱 Status Order {service_name} {status_emoji}\n"
                        message += f"🆔 Order ID: {order_id}\n"
                        message += f"📞 Nomor: <code>{phone_number}</code> | <code>{phoneformat62}</code>\n"
                        message += f"💵 Harga: {price}\n"
                        message += f"📊 Status: {order_status}\n"
                        message += f"📧 Menunggu SMS...\n"
                        message += f"🕒 Dipesan pada: {order_time}"
                    else:
                        message = f"📱 SMS Order {service_name} {status_emoji}\n"
                        message += f"🆔 Order ID: {order_id}\n"
                        message += f"📞 Nomor: <code>{phone_number}</code> | <code>{phoneformat62}</code>\n"
                        message += f"💵 Harga: {price}\n"
                        message += f"📊 Status: {order_status}\n"
                        message += f"📧 Total SMS: {len(sms_data)}\n"
                        message += f"🕒 Dipesan pada: {order_time}\n\n"

                        for i, sms in enumerate(sms_data, 1):
                            sms_text = sms.get(
                                'sms', sms.get('fullSms', 'No text'))
                            full_sms = sms.get('fullSms', sms_text)

                            if sms_text:
                                sms_text = sms_text.strip()
                            if full_sms:
                                full_sms = full_sms.strip()

                            message += f"📧 SMS #{i}: <code>{sms_text}</code>\n"
                            if full_sms and full_sms != sms_text and len(full_sms) > len(sms_text):
                                message += f"📄 Full SMS: <code>{full_sms}</code>\n"
                            message += "\n"

                    # Pending order buttons
                    action_buttons = [
                        [
                            InlineKeyboardButton(
                                "❌ Batalkan OTP", callback_data=f"cancel_order_{order_id}"),
                            InlineKeyboardButton(
                                "✅ Tandai Selesai", callback_data=f"finish_order_{order_id}")
                        ],
                        [
                            InlineKeyboardButton(
                                "🔄 Resend", callback_data=f"resend_order_{order_id}"),
                            InlineKeyboardButton(
                                "📱 Get SMS", callback_data=f"get_sms_{order_id}")
                        ]
                    ]
                    if service_id != "N/A":
                        action_buttons.append([InlineKeyboardButton(
                            f"🔄 Order Again", callback_data=f"order_service_{service_id}")])
                    reply_markup = InlineKeyboardMarkup(action_buttons)

                # Edit the message
                try:
                    await context.bot.edit_message_text(
                        chat_id=query.message.chat_id,
                        message_id=query.message.message_id,
                        text=message,
                        parse_mode="HTML",
                        reply_markup=reply_markup
                    )
                except Exception as edit_error:
                    logger.error(
                        f"Failed to edit message for get_sms {order_id}: {str(edit_error)}")
                    await query.answer(f"📱 SMS status updated for order #{order_id}", show_alert=True)

            else:
                await query.answer(f"⚠️ Error: {data.get('message', 'No data found')}", show_alert=True)
        else:
            await query.answer(f"❌ HTTP Error {status_code}", show_alert=True)
    except Exception as e:
        await query.answer(f"❌ Error: {str(e)}", show_alert=True)

    logger.info(f"User {user_id} checked SMS for order {order_id}")


async def cancel_order(query, context: ContextTypes.DEFAULT_TYPE, order_id: str) -> None:
    """Enhanced async cancel order function"""
    user_id = str(query.from_user.id)

    # Stop all monitoring and timers for this order
    if order_id in active_order_monitors:
        monitor_info = active_order_monitors[order_id]
        if isinstance(monitor_info, dict) and 'task' in monitor_info:
            monitor_info['task'].cancel()
        del active_order_monitors[order_id]

    if order_id in pending_cancellations:
        pending_task = pending_cancellations[order_id]
        if isinstance(pending_task, dict) and 'task' in pending_task:
            pending_task['task'].cancel()
        del pending_cancellations[order_id]

    if order_id in auto_cancel_timers:
        auto_task = auto_cancel_timers[order_id]
        if isinstance(auto_task, dict) and 'task' in auto_task:
            auto_task['task'].cancel()
        del auto_cancel_timers[order_id]

    # Extract order info
    original_message = query.message
    service_name = "Unknown Service"
    phone_number = "N/A"
    order_time = "N/A"

    if original_message and original_message.text:
        service_info = extract_service_info_from_message(original_message.text)
        service_name = service_info['service_name']
        phone_number = service_info['phone_number']
        order_time = service_info['order_time']

    # Determine cancellation reason and timing
    cancel_reason = "Pembatalan manual oleh user"
    if "❌ Tidak Terdaftar" in original_message.text:
        cancel_reason = "Nomor tidak terdaftar pada e-wallet"
    elif "✅ Terdaftar" in original_message.text:
        cancel_reason = "Nomor sudah terdaftar pada e-wallet"

    # Calculate delay
    current_time = datetime.now()
    delay_seconds = 120
    should_cancel_immediately = False

    if order_time != "N/A":
        try:
            order_datetime = datetime.strptime(order_time, '%Y-%m-%d %H:%M:%S')
            time_since_order = current_time - order_datetime
            if time_since_order.total_seconds() >= 130:
                should_cancel_immediately = True
                delay_seconds = 5
        except:
            pass

    # Create immediate message
    if should_cancel_immediately:
        immediate_message = (
            f"⚡ Membatalkan pesanan {service_name}...\n"
            f"🆔 Order ID: {order_id}\n"
            f"📞 Nomor: {phone_number}\n"
            f"🚫 Alasan: {cancel_reason}\n"
            f"🕒 Dipesan pada: {order_time}\n"
            f"⏰ Order sudah lebih dari 2 menit, dibatalkan langsung\n"
            f"🔄 Status: Memproses pembatalan..."
        )
    else:
        from datetime import timedelta
        cancel_time = (
            current_time + timedelta(seconds=delay_seconds)).strftime('%H:%M:%S')
        immediate_message = (
            f"⏳ Proses pembatalan pesanan {service_name}\n"
            f"🆔 Order ID: {order_id}\n"
            f"📞 Nomor: {phone_number}\n"
            f"🚫 Alasan: {cancel_reason}\n"
            f"🕒 Dipesan pada: {order_time}\n"
            f"⏰ Akan dibatalkan pada: {cancel_time}\n"
            f"🔄 Status: Menunggu konfirmasi API..."
        )

    # Edit message immediately
    try:
        await context.bot.edit_message_text(
            chat_id=query.message.chat_id,
            message_id=query.message.message_id,
            text=immediate_message,
            parse_mode="HTML"
        )
    except Exception as edit_error:
        logger.error(
            f"Failed to edit message immediately for order {order_id}: {str(edit_error)}")

    # Schedule the actual cancellation
    await schedule_delayed_cancellation(
        context, order_id, delay_seconds=delay_seconds,
        message_id=query.message.message_id, chat_id=query.message.chat_id
    )

    if should_cancel_immediately:
        await query.answer("⚡ Order sudah lebih dari 2 menit, dibatalkan langsung!", show_alert=True)
    else:
        await query.answer("⏳ Pembatalan akan diproses dalam 2 menit...", show_alert=True)

    logger.info(
        f"User {user_id} requested manual cancellation for order {order_id}")


async def finish_order(query, context: ContextTypes.DEFAULT_TYPE, order_id: str) -> None:
    """Enhanced async finish order function"""
    user_id = str(query.from_user.id)
    url = f"{BASE_URL}order/{order_id}/3"  # Status 3 = Finish
    headers = {"X-Api-Key": API_KEY}

    try:
        # Use enhanced async client
        status_code, data = await http_client.patch(url, headers=headers)

        if status_code != 200:
            await query.answer(f"❌ HTTP Error {status_code}", show_alert=True)
            return

        if not data or not data.get('status'):
            await query.answer(f"⚠️ Error: {data.get('message', 'Failed to finish order') if data else 'Unknown error'}", show_alert=True)
            return

    except Exception as e:
        await query.answer(f"❌ Error: {str(e)}", show_alert=True)
        return

    # Stop all monitoring and timers
    tasks_to_cancel = [
        (active_order_monitors, "monitoring"),
        (pending_cancellations, "pending cancellation"),
        (auto_cancel_timers, "auto-cancel timer"),
        (user_cancel_requests, "user cancel request")
    ]

    for task_dict, task_name in tasks_to_cancel:
        if order_id in task_dict:
            task_info = task_dict[order_id]
            if isinstance(task_info, dict) and 'task' in task_info:
                task_info['task'].cancel()
            del task_dict[order_id]
            logger.info(f"Cancelled {task_name} for order {order_id}")

    # Get order information
    stored_order = get_order_info(order_id)
    service_name = stored_order.get('service_name', 'Unknown Service')
    phone_number = stored_order.get('phone_number', 'N/A')
    order_time = stored_order.get('order_time', 'N/A')
    price = stored_order.get('price', 'N/A')
    service_id = stored_order.get('service_id', 'N/A')

    # Use message extraction as fallback
    if service_name == 'Unknown Service' or phone_number == 'N/A' or order_time == 'N/A':
        original_message = query.message
        if original_message and original_message.text:
            service_info = extract_service_info_from_message(
                original_message.text)
            if service_name == 'Unknown Service':
                service_name = service_info['service_name']
            if phone_number == 'N/A':
                phone_number = service_info['phone_number']
            if order_time == 'N/A':
                order_time = service_info['order_time']
            if price == 'N/A':
                price = service_info['price']

    # Try to get service name from file if still unknown
    if service_name == 'Unknown Service' and service_id != 'N/A':
        try:
            with open("serviceotp.txt", "r", encoding='utf-8') as f:
                for line in f:
                    parts = line.strip().split(maxsplit=1)
                    if len(parts) == 2 and parts[0] == service_id:
                        service_name = parts[1]
                        break
        except FileNotFoundError:
            pass

    if service_name == 'Unknown Service':
        service_name = "Manual Completion"

    # Format display values
    if price != 'N/A' and isinstance(price, (int, float)):
        price = f"${float(price):.5f}"
    elif price == 'N/A':
        price = "$0.00000"

    if phone_number != 'N/A':
        phoneformat62 = phone_number.replace(
            "62", "0") if phone_number.startswith("62") else phone_number
        phone_display = f"<code>{phone_number}</code> | <code>{phoneformat62}</code>"
    else:
        phone_display = "N/A"

    # Save to completion file
    try:
        clean_service_name = "".join(
            c for c in service_name if c.isalnum() or c in (' ', '-', '_')).rstrip()
        clean_service_name = clean_service_name.replace(' ', '_')
        filename = f"{clean_service_name}selesai.txt"

        if not os.path.exists(filename):
            with open(filename, "w", encoding='utf-8') as f:
                f.write(
                    "timestamp,user_id,order_id,service_name,phone_number,price,completion_type\n")

        with open(filename, "a", encoding='utf-8') as f:
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            completion_entry = f"{timestamp},{user_id},{order_id},{service_name},{phone_number},{price},manual_finish\n"
            f.write(completion_entry)

        logger.info(f"Saved completed order {order_id} to {filename}")
    except Exception as save_error:
        logger.error(
            f"Failed to save completed order {order_id}: {str(save_error)}")

    # Create completion message
    completion_message = (
        f"✅ Pesanan selesai - {service_name}\n"
        f"🆔 Order ID: {order_id}\n"
        f"📞 Nomor: {phone_display}\n"
        f"💵 Harga: {price}\n"
        f"🕒 Dipesan pada: {order_time}\n"
        f"✅ Diselesaikan pada: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"🎉 Status: Berhasil diselesaikan manual"
    )

    # Edit the message
    try:
        await context.bot.edit_message_text(
            chat_id=query.message.chat_id,
            message_id=query.message.message_id,
            text=completion_message,
            parse_mode="HTML"
        )
        await query.answer("✅ Pesanan berhasil ditandai selesai!", show_alert=True)
    except Exception as edit_error:
        logger.error(
            f"Failed to edit message for finished order {order_id}: {str(edit_error)}")
        await query.answer("✅ Pesanan ditandai selesai!", show_alert=True)

    logger.info(f"User {user_id} marked order {order_id} as finished")


async def resend_order(query, context: ContextTypes.DEFAULT_TYPE, order_id: str) -> None:
    """Enhanced async resend order function"""
    user_id = str(query.from_user.id)
    url = f"{BASE_URL}order/{order_id}/2"  # Status 2 = Resend
    headers = {"X-Api-Key": API_KEY}

    try:
        # Use enhanced async client
        status_code, data = await http_client.patch(url, headers=headers)

        if status_code == 200 and data:
            if data.get('status'):
                # Extract info from original message
                original_message = query.message
                service_info = extract_service_info_from_message(
                    original_message.text)
                service_name = service_info['service_name']
                phone_number = service_info['phone_number']
                price = service_info['price']
                order_time = service_info['order_time']

                # Create resend message
                resend_message = (
                    f"🔄 Resend Order {service_name}\n"
                    f"🆔 Order ID: {order_id}\n"
                    f"📞 Nomor: {phone_number}\n"
                    f"💵 Harga: {price}\n"
                    f"📊 Status: Resend berhasil\n"
                    f"🕒 Dipesan pada: {order_time}\n"
                    f"🔄 Resend pada: {datetime.now().strftime('%H:%M:%S')}\n"
                    f"⏳ Menunggu SMS..."
                )

                # Recreate action buttons
                service_id = "N/A"
                if hasattr(query, 'data') and query.data:
                    if 'order_service_' in str(query.data):
                        service_id = str(query.data).split('_')[-1]

                action_buttons = [
                    [
                        InlineKeyboardButton(
                            "❌ Batalkan OTP", callback_data=f"cancel_order_{order_id}"),
                        InlineKeyboardButton(
                            "✅ Tandai Selesai", callback_data=f"finish_order_{order_id}")
                    ],
                    [
                        InlineKeyboardButton(
                            "🔄 Resend", callback_data=f"resend_order_{order_id}"),
                        InlineKeyboardButton(
                            "📱 Get SMS", callback_data=f"get_sms_{order_id}")
                    ]
                ]

                if service_id != "N/A":
                    action_buttons.append([InlineKeyboardButton(
                        f"🔄 Order Again", callback_data=f"order_service_{service_id}")])

                reply_markup = InlineKeyboardMarkup(action_buttons)

                # Edit the message
                try:
                    await context.bot.edit_message_text(
                        chat_id=query.message.chat_id,
                        message_id=query.message.message_id,
                        text=resend_message,
                        parse_mode="HTML",
                        reply_markup=reply_markup
                    )
                    await query.answer("🔄 Resend berhasil dikirim!", show_alert=True)
                except Exception as edit_error:
                    logger.error(
                        f"Failed to edit message for resend order {order_id}: {str(edit_error)}")
                    await query.answer("🔄 Resend berhasil dikirim!", show_alert=True)

                # Restart monitoring for this order
                await monitor_order_sms(
                    context, order_id, query.message.message_id, query.message.chat_id, initial_sms_count=0
                )

            else:
                await query.answer(f"❌ Gagal resend: {data.get('message', 'Unknown error')}", show_alert=True)
        else:
            await query.answer(f"❌ HTTP Error {status_code}", show_alert=True)
    except Exception as e:
        await query.answer(f"❌ Error: {str(e)}", show_alert=True)

    logger.info(f"User {user_id} requested resend for order {order_id}")

# Enhanced button callback handler


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    log_user_id(user_id)

    if user_id not in AUTHORIZED_IDS:
        await query.message.reply_text("🚫 Access denied! Please contact an admin to gain access. 😊")
        logger.warning(
            f"Unauthorized button access attempt by user ID: {user_id}")
        return

    # Enhanced callback handling with better error handling
    try:
        if query.data == "balance":
            await balance_callback(query, context)
        elif query.data == "services":
            await services_callback(query, context)
        elif query.data == "cekprice":
            waiting_for_service_id[user_id] = True
            await query.message.reply_text("🔍 Please send the service ID to check its price (e.g., 399):")
        elif query.data == "order":
            await order_callback(query, context)
        elif query.data == "active_orders":
            await active_orders_callback(query, context)
        elif query.data.startswith("order_service_"):
            service_id = query.data.split("_")[2]
            await place_order(query, context, service_id)
        elif query.data == "admin_tools" and user_id in ADMIN_IDS:
            admin_keyboard = [
                [InlineKeyboardButton("🔐 Add User", callback_data="add_user")],
                [InlineKeyboardButton(
                    "🗑️ Delete User", callback_data="delete_user")],
                [InlineKeyboardButton(
                    "➕ Add Service", callback_data="add_service")],
                [InlineKeyboardButton("➖ Delete Service",
                                      callback_data="delete_service")]
            ]
            reply_markup = InlineKeyboardMarkup(admin_keyboard)
            await query.message.reply_text("🔧 Admin Tools:", reply_markup=reply_markup)
        elif query.data == "add_user" and user_id in ADMIN_IDS:
            waiting_for_user_id[user_id] = 'add'
            await query.message.reply_text("🔐 Please send the User ID you want to add:")
        elif query.data == "delete_user" and user_id in ADMIN_IDS:
            await show_list(query, context, 'user')
        elif query.data == "add_service" and user_id in ADMIN_IDS:
            waiting_for_service_input[user_id] = 'add'
            await query.message.reply_text("➕ Please send the service ID and name in format: <service_id> <service_name>")
        elif query.data == "delete_service" and user_id in ADMIN_IDS:
            await show_list(query, context, 'service')
        elif query.data.startswith("get_sms_"):
            order_id = query.data.split("_")[2]
            await get_sms(query, context, order_id)
        elif query.data.startswith("cancel_order_"):
            order_id = query.data.split("_")[2]
            await cancel_order(query, context, order_id)
        elif query.data.startswith("finish_order_"):
            order_id = query.data.split("_")[2]
            await finish_order(query, context, order_id)
        elif query.data.startswith("resend_order_"):
            order_id = query.data.split("_")[2]
            await resend_order(query, context, order_id)
        # Add more callback handlers as needed...
        else:
            await query.answer("❌ Unknown command", show_alert=True)

    except Exception as e:
        logger.error(f"Error in button callback for user {user_id}: {str(e)}")
        await query.answer("❌ An error occurred. Please try again.", show_alert=True)

# Enhanced async check price


async def check_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    if not await check_authorized(update, context):
        return

    if not context.args:
        await update.message.reply_text("🔍 Please provide a service ID: /cekprice <service_id>\nExample: /cekprice 399")
        return

    service_id = context.args[0]
    if not service_id.isdigit():
        await update.message.reply_text("❌ Please provide a valid numeric service ID!")
        return

    # Send loading message
    loading_msg = await update.message.reply_text("⏳ Checking price...")

    url = f"{BASE_URL}price/{service_id}/"
    headers = {"X-Api-Key": API_KEY}

    try:
        status_code, data = await http_client.get(url, headers)

        if status_code == 200 and data:
            if data.get('status') and data.get('data'):
                price_data = data['data']
                message = f"💰 **Price Information for Service ID: {service_id}**\n\n"

                for item in price_data:
                    country_name = item.get('countryName', 'Unknown')
                    country_id = item.get('country', 'N/A')
                    available = item.get('available', 0)
                    price_usd = item.get('priceUsd', 0)
                    custom_prices = item.get('customPrice', [])

                    message += f"🌍 **{country_name}** (ID: {country_id})\n"
                    message += f"📱 Available: {available:,} numbers\n"
                    message += f"💵 Standard Price: ${price_usd:.5f} USD\n"

                    if custom_prices:
                        message += f"🎯 **Custom Prices:**\n"
                        for i, custom_price in enumerate(custom_prices[:5], 1):
                            if isinstance(custom_price, dict):
                                custom_amount = custom_price.get(
                                    'price', custom_price.get('amount', 'N/A'))
                                custom_type = custom_price.get(
                                    'type', custom_price.get('name', f'Option {i}'))
                                message += f"   • {custom_type}: ${custom_amount:.5f} USD\n"
                            else:
                                message += f"   • Custom {i}: ${custom_price:.5f} USD\n"
                    else:
                        message += f"🎯 Custom Prices: Not available\n"

                    message += f"\n" + "─" * 30 + "\n"

                message += f"🕒 Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                await loading_msg.edit_text(message, parse_mode="Markdown")
            else:
                await loading_msg.edit_text(f"⚠️ Error: {data.get('message', 'No price data available')}")
        else:
            await loading_msg.edit_text(f"❌ HTTP Error {status_code}")
    except Exception as e:
        await loading_msg.edit_text(f"❌ Error: {str(e)}")

    logger.info(f"User {user_id} checked price for service ID: {service_id}")


async def main_async() -> None:
    """Enhanced async main function with comprehensive error handling"""
    try:
        # Load order storage on startup
        load_order_storage()
        logger.info("📦 Order storage loaded successfully")

        # Create the Application with enhanced settings
        application = (Application.builder()
                       .token(TELEGRAM_TOKEN)
                       # Enable concurrent processing
                       .concurrent_updates(True)
                       .build())

        # Add all handlers
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("balance", balance))
        application.add_handler(CommandHandler("services", services))
        application.add_handler(CommandHandler("cekprice", check_price))
        application.add_handler(CommandHandler("order", order))
        application.add_handler(CommandHandler("active_orders", active_orders))
        application.add_handler(CommandHandler("adduser", add_user))
        application.add_handler(CommandHandler("admin", admin))
        application.add_handler(CallbackQueryHandler(button_callback))
        application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND, handle_text_message))

        logger.info("🎯 All handlers registered successfully")

        # Run the bot with enhanced async processing
        logger.info("🚀 Enhanced Bot starting up with async optimization...")
        await application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,  # Drop pending updates on startup
            close_loop=False
        )

    except Exception as e:
        logger.error(f"Critical error in main_async: {str(e)}")
        raise
    finally:
        # Cleanup
        await http_client.close()
        logger.info("🧹 Cleanup completed")


def run_flask():
    """Run Flask app in a separate thread for health checks and UptimeRobot"""
    try:
        app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
    except Exception as e:
        logger.error(f"Flask server error: {str(e)}")


def main() -> None:
    """Main entry point with Flask integration for Render.com deployment"""
    try:
        # Start Flask in a separate thread for health checks
        flask_thread = Thread(target=run_flask, daemon=True)
        flask_thread.start()
        logger.info(f"🌐 Flask health check server started on port {PORT}")
        logger.info(
            f"📡 Health endpoints: http://localhost:{PORT}/ and /health")

        # Run the async bot
        asyncio.run(main_async())

    except KeyboardInterrupt:
        logger.info("🛑 Bot stopped by user")
    except Exception as e:
        logger.error(f"❌ Bot error: {str(e)}")
        raise


if __name__ == "__main__":
    main()
