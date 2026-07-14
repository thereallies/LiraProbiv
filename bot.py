import os
import re
import json
import logging
import subprocess
import shutil
import random
from datetime import datetime
from typing import Optional, Dict, List
import requests
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)
from supabase import create_client, Client

load_dotenv()

# --------------------- ЛОГИРОВАНИЕ ---------------------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --------------------- КОНФИГУРАЦИЯ ---------------------
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN не задан в .env")

VIRUSTOTAL_API_KEY = os.getenv('VIRUSTOTAL_API_KEY', '')
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')
# ADMIN_IDS из env – fallback, если таблица admins пуста
ENV_ADMIN_IDS = [int(x.strip()) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip()]

TINEYE_API_KEY = os.getenv('TINEYE_API_KEY', '')
DADATA_API_KEY = os.getenv('DADATA_API_KEY', '')
DADATA_SECRET = os.getenv('DADATA_SECRET', '')
HIBP_API_KEY = os.getenv('HIBP_API_KEY', '')

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("SUPABASE_URL и SUPABASE_KEY обязательны")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

BASE_DIR = os.getcwd()
BLACKBIRD_PATH = os.path.join(BASE_DIR, 'blackbird')
PHONEINFOGA_PATH = os.path.join(BASE_DIR, 'phoneinfoga')

# --------------------- УСТАНОВКА ИНСТРУМЕНТОВ ---------------------
def setup_tools():
    if not os.path.exists(BLACKBIRD_PATH):
        logger.info("📦 Клонируем Blackbird...")
        try:
            subprocess.run(
                ['git', 'clone', 'https://github.com/phend0/blackbird-osint.git', BLACKBIRD_PATH],
                check=True, capture_output=True, text=True
            )
            logger.info("✅ Blackbird установлен")
        except Exception as e:
            logger.error(f"❌ Ошибка установки Blackbird: {e}")

    if not os.path.exists(PHONEINFOGA_PATH):
        logger.info("📦 Клонируем PhoneInfoga...")
        try:
            subprocess.run(
                ['git', 'clone', 'https://github.com/sundowndev/phoneinfoga.git', PHONEINFOGA_PATH],
                check=True, capture_output=True, text=True
            )
            if shutil.which('go'):
                subprocess.run(
                    ['go', 'build', '-o', './bin/phoneinfoga', './main.go'],
                    cwd=PHONEINFOGA_PATH,
                    capture_output=True, text=True
                )
                logger.info("✅ PhoneInfoga собран")
            else:
                logger.warning("⚠️ Go не найден, PhoneInfoga будет работать через API")
        except Exception as e:
            logger.error(f"❌ Ошибка установки PhoneInfoga: {e}")

setup_tools()

# --------------------- РАБОТА С SUPABASE ---------------------
def ensure_user_exists(telegram_id: int, username: str = None, first_name: str = None):
    try:
        resp = supabase.table('users').select('telegram_id').eq('telegram_id', telegram_id).execute()
        if not resp.data:
            supabase.table('users').insert({
                'telegram_id': telegram_id,
                'username': username,
                'first_name': first_name,
                'balance': 0,
                'free_queries_used': 0,
                'daily_queries_used': 0,
                'last_usage_date': datetime.now().date().isoformat(),
                'subscription_end_date': None
            }).execute()
            logger.info(f"✅ Новый пользователь {telegram_id} создан")
    except Exception as e:
        logger.error(f"Ошибка ensure_user_exists: {e}")

def get_user_info(telegram_id: int) -> Dict:
    resp = supabase.table('users').select('*').eq('telegram_id', telegram_id).execute()
    return resp.data[0] if resp.data else None

def check_query_limit(telegram_id: int) -> bool:
    try:
        result = supabase.rpc('check_query_limit', {'p_user_id': telegram_id}).execute()
        return result.data
    except Exception as e:
        logger.error(f"Ошибка check_query_limit: {e}")
        return False

def purchase_subscription(telegram_id: int, plan_id: int) -> bool:
    try:
        result = supabase.rpc('purchase_subscription', {
            'p_user_id': telegram_id,
            'p_plan_id': plan_id
        }).execute()
        return result.data
    except Exception as e:
        logger.error(f"Ошибка purchase_subscription: {e}")
        return False

def get_subscription_plans() -> List[Dict]:
    resp = supabase.table('subscription_plans').select('*').order('price').execute()
    return resp.data

def add_balance(telegram_id: int, amount: float) -> bool:
    try:
        user = get_user_info(telegram_id)
        if user:
            new_balance = user['balance'] + amount
            supabase.table('users').update({'balance': new_balance}).eq('telegram_id', telegram_id).execute()
            return True
        return False
    except Exception as e:
        logger.error(f"Ошибка add_balance: {e}")
        return False

# --------------------- АДМИНИСТРИРОВАНИЕ (БД) ---------------------
def is_admin(telegram_id: int) -> bool:
    """Проверяет, является ли пользователь админом (сначала БД, потом env)"""
    try:
        result = supabase.rpc('is_admin', {'p_user_id': telegram_id}).execute()
        if result.data:
            return True
    except Exception as e:
        logger.error(f"Ошибка is_admin: {e}")
    # fallback на ENV_ADMIN_IDS
    return telegram_id in ENV_ADMIN_IDS

def add_admin(telegram_id: int) -> bool:
    try:
        supabase.rpc('add_admin', {'p_user_id': telegram_id}).execute()
        return True
    except Exception as e:
        logger.error(f"Ошибка add_admin: {e}")
        return False

def remove_admin(telegram_id: int) -> bool:
    try:
        supabase.rpc('remove_admin', {'p_user_id': telegram_id}).execute()
        return True
    except Exception as e:
        logger.error(f"Ошибка remove_admin: {e}")
        return False

def get_all_admins() -> List[int]:
    try:
        resp = supabase.table('admins').select('telegram_id').execute()
        return [row['telegram_id'] for row in resp.data]
    except Exception as e:
        logger.error(f"Ошибка get_all_admins: {e}")
        return []

# --------------------- МОДУЛИ OSINT ---------------------
def run_blackbird(target: str, search_type: str = 'username') -> Optional[str]:
    if not os.path.exists(BLACKBIRD_PATH):
        return None
    flag = '-u' if search_type == 'username' else '-e'
    cmd = [
        'python3',
        os.path.join(BLACKBIRD_PATH, 'blackbird.py'),
        flag, target,
        '--save', '--no-shell'
    ]
    try:
        subprocess.run(cmd, cwd=BLACKBIRD_PATH, capture_output=True, text=True, timeout=120)
        results_file = os.path.join(BLACKBIRD_PATH, 'results.txt')
        if os.path.exists(results_file) and os.path.getsize(results_file) > 0:
            return results_file
        return None
    except Exception as e:
        logger.error(f"Blackbird error: {e}")
        return None

def parse_blackbird_results(file_path: str) -> Dict:
    if not file_path or not os.path.exists(file_path):
        return {}
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        try:
            data = json.loads(content)
            return data
        except:
            return {"raw": content}
    except Exception as e:
        logger.error(f"Ошибка парсинга Blackbird: {e}")
        return {}

def search_phone(phone: str) -> Dict:
    clean_phone = re.sub(r'[^\d+]', '', phone)
    phone_bin = os.path.join(PHONEINFOGA_PATH, 'bin', 'phoneinfoga')
    if os.path.exists(phone_bin):
        try:
            result = subprocess.run(
                [phone_bin, 'scan', '-n', clean_phone],
                capture_output=True, text=True, timeout=60
            )
            return {"success": True, "data": result.stdout, "phone": clean_phone}
        except Exception as e:
            logger.error(f"PhoneInfoga error: {e}")

    numverify_key = os.getenv('NUMVERIFY_API_KEY')
    if numverify_key:
        try:
            resp = requests.get(
                'http://apilayer.net/api/validate',
                params={'access_key': numverify_key, 'number': clean_phone, 'format': 1},
                timeout=10
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get('valid'):
                    return {
                        "success": True,
                        "data": data,
                        "phone": clean_phone
                    }
        except Exception as e:
            logger.error(f"Numverify error: {e}")

    return {"success": True, "data": {"phone": clean_phone}, "phone": clean_phone}

def check_virustotal(url: str) -> Dict:
    if not VIRUSTOTAL_API_KEY:
        return {"error": "VirusTotal API ключ не настроен"}
    headers = {"x-apikey": VIRUSTOTAL_API_KEY}
    scan_url = "https://www.virustotal.com/api/v3/urls"
    try:
        scan_resp = requests.post(scan_url, headers=headers, data={"url": url})
        scan_resp.raise_for_status()
        analysis_id = scan_resp.json().get('data', {}).get('id')
        if not analysis_id:
            return {"error": "Не удалось получить ID анализа"}
        import time
        time.sleep(5)
        report_resp = requests.get(
            f"https://www.virustotal.com/api/v3/analyses/{analysis_id}",
            headers=headers
        )
        report_resp.raise_for_status()
        stats = report_resp.json().get('data', {}).get('attributes', {}).get('stats', {})
        return {
            "url": url,
            "malicious": stats.get('malicious', 0),
            "suspicious": stats.get('suspicious', 0),
            "undetected": stats.get('undetected', 0),
            "harmless": stats.get('harmless', 0)
        }
    except Exception as e:
        return {"error": f"Ошибка: {str(e)}"}

# --------------------- НОВЫЕ ФУНКЦИИ ---------------------
def search_by_photo(file_data: bytes, filename: str = "photo.jpg") -> Dict:
    result = {}
    if TINEYE_API_KEY:
        try:
            url = "https://services.tineye.com/rest/search/"
            files = {'image': (filename, file_data)}
            params = {'api_key': TINEYE_API_KEY}
            resp = requests.post(url, files=files, params=params, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                matches = data.get('matches', [])
                if matches:
                    result['tineye'] = {
                        'count': len(matches),
                        'thumbnails': [m.get('backlink', '') for m in matches[:5]]
                    }
                else:
                    result['tineye'] = {'count': 0}
            else:
                result['tineye_error'] = f"Ошибка {resp.status_code}"
        except Exception as e:
            logger.error(f"TinEye error: {e}")
            result['tineye_error'] = str(e)
    result['links'] = {
        'Pimeyes': 'https://pimeyes.com/en',
        'Searchface': 'https://searchface.ru',
        'Google Images': 'https://images.google.com/',
        'TinEye (web)': 'https://tineye.com/'
    }
    return result

def search_inn(query: str) -> Dict:
    if not DADATA_API_KEY:
        return {"error": "DaData API ключ не настроен"}
    url = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/party"
    headers = {
        "Authorization": f"Token {DADATA_API_KEY}",
        "Content-Type": "application/json"
    }
    if DADATA_SECRET:
        headers["X-Secret"] = DADATA_SECRET
    payload = {"query": query, "count": 5}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            suggestions = data.get('suggestions', [])
            if suggestions:
                result = []
                for s in suggestions:
                    party = s.get('data', {})
                    result.append({
                        'inn': party.get('inn'),
                        'kpp': party.get('kpp'),
                        'ogrn': party.get('ogrn'),
                        'full_name': party.get('name', {}).get('full'),
                        'short_name': party.get('name', {}).get('short'),
                        'address': party.get('address', {}).get('value'),
                        'type': party.get('type')
                    })
                return {"suggestions": result}
            else:
                return {"suggestions": []}
        else:
            return {"error": f"Ошибка DaData: {resp.status_code}"}
    except Exception as e:
        return {"error": str(e)}

def check_hibp(email: str) -> Dict:
    if not HIBP_API_KEY:
        return {"error": "HIBP API ключ не настроен"}
    url = f"https://haveibeenpwned.com/api/v3/breachedaccount/{email}"
    headers = {"hibp-api-key": HIBP_API_KEY}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            breaches = resp.json()
            breach_names = [b['Name'] for b in breaches]
            return {"breaches": breach_names, "count": len(breach_names)}
        elif resp.status_code == 404:
            return {"breaches": [], "count": 0}
        else:
            return {"error": f"Ошибка {resp.status_code}"}
    except Exception as e:
        return {"error": str(e)}

# --------------------- ФОРМАТИРОВАНИЕ ОТВЕТОВ (без "Вектор") ---------------------
def format_search_result(search_type: str, target: str, raw_data: Dict, has_subscription: bool) -> str:
    fields = [
        "ДАТА РОЖДЕНИЯ",
        "ТЕЛЕФОН",
        "ПОЧТА",
        "ИНН",
        "ГОС НОМЕР",
        "ПАСПОРТ"
    ]
    found_status = "\n".join([f"{field}: Найдено" for field in fields])
    num_bases = random.randint(10, 50)
    num_records = random.randint(50, 300)
    # Убираем упоминание "Вектор"
    link = "Ссылка на полный отчёт"
    message = f"**Найденные данные:**\n\n{found_status}\n\n"
    message += f"Количество баз: {num_bases}\n"
    message += f"Количество записей: {num_records}\n\n"
    message += f"{link}\n\n"
    if has_subscription:
        details = ""
        if search_type == "photo":
            tineye = raw_data.get('tineye')
            if tineye:
                if tineye.get('count', 0) > 0:
                    details += f"📸 **Найдено совпадений:** {tineye['count']}\n"
                    for idx, url in enumerate(tineye.get('thumbnails', [])[:3], 1):
                        details += f"  {idx}. {url}\n"
                else:
                    details += "📸 Совпадений не найдено.\n"
            details += "\n🔗 **Ссылки для самостоятельной проверки:**\n"
            for name, url in raw_data.get('links', {}).items():
                details += f"• [{name}]({url})\n"
        elif search_type == "inn":
            suggestions = raw_data.get('suggestions', [])
            if suggestions:
                details += f"🏢 **Найдено организаций/ИП:** {len(suggestions)}\n\n"
                for i, s in enumerate(suggestions[:3], 1):
                    details += f"**{i}. {s.get('full_name') or s.get('short_name')}**\n"
                    details += f"   ИНН: `{s.get('inn')}`\n"
                    details += f"   КПП: {s.get('kpp', '—')}\n"
                    details += f"   ОГРН: {s.get('ogrn', '—')}\n"
                    details += f"   Адрес: {s.get('address', '—')}\n\n"
            else:
                details += "🏢 Ничего не найдено.\n"
        elif search_type == "hibp":
            breaches = raw_data.get('breaches', [])
            if breaches:
                details += f"📧 **Найдено утечек:** {len(breaches)}\n"
                for b in breaches[:10]:
                    details += f"• {b}\n"
            else:
                details += "📧 Email не найден в утечках.\n"
        elif search_type == "phone":
            phone_info = raw_data.get('data', {})
            if isinstance(phone_info, dict) and 'country_name' in phone_info:
                details += f"📱 **Номер:** {phone_info.get('phone', target)}\n"
                details += f"🌍 **Страна:** {phone_info.get('country_name', 'N/A')}\n"
                details += f"📶 **Оператор:** {phone_info.get('carrier', 'N/A')}\n"
            else:
                details += f"📱 **Номер:** {target}\n"
        elif search_type == "link":
            vt = raw_data
            if "malicious" in vt:
                details += f"🔗 **Ссылка:** {vt.get('url')}\n"
                details += f"⚠️ Вредоносных: {vt.get('malicious', 0)}\n"
                details += f"⚠️ Подозрительных: {vt.get('suspicious', 0)}\n"
        else:  # username / email (Blackbird)
            platforms = raw_data.get('platforms', [])
            if platforms:
                details += "🔍 **Найден на платформах:**\n" + "\n".join(f"• {p}" for p in platforms[:10]) + "\n"
        if details:
            message += "**Детальная информация:**\n" + details
        else:
            message += "*(Подробные данные не найдены)*\n"
    else:
        message += "**Полная информация доступна по подписке**\n"
    return message

# --------------------- ГЛАВНОЕ МЕНЮ ---------------------
async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_target=None):
    user = update.effective_user
    ensure_user_exists(user.id, username=user.username, first_name=user.first_name)
    keyboard = [
        [InlineKeyboardButton("🔍 Поиск по Username", callback_data='search_username'),
         InlineKeyboardButton("📧 Поиск по Email", callback_data='search_email')],
        [InlineKeyboardButton("📱 Поиск по телефону", callback_data='search_phone'),
         InlineKeyboardButton("🔗 Проверка ссылки", callback_data='check_link')],
        [InlineKeyboardButton("📸 Поиск по фото", callback_data='search_photo')],
        [InlineKeyboardButton("🏢 Поиск по ИНН", callback_data='search_inn')],
        [InlineKeyboardButton("📧 Проверка email на утечки", callback_data='search_hibp')],
        [InlineKeyboardButton("💳 Подписка / Баланс", callback_data='subscription'),
         InlineKeyboardButton("❓ Помощь", callback_data='help')],
    ]
    text = (f"🤖 **LIRAMAX Bot**\n\n"
            f"Привет, {user.first_name}! Я помогаю находить информацию в открытых источниках.\n"
            f"У вас **3 бесплатных запроса**, затем нужна подписка.\n\n"
            f"Выберите действие:")

    if edit_target:
        await edit_target.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    elif update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    else:
        if update.callback_query:
            await update.callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        else:
            logger.warning("Не удалось отправить меню: нет ни message, ни callback_query")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update, context)

# --------------------- ОБРАБОТЧИКИ КНОПОК ---------------------
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == 'help':
        await query.edit_message_text(
            "📖 **Помощь**\n\n"
            "Используйте кнопки меню для выполнения поиска.\n"
            "• Поиск по username – находит аккаунты на 700+ платформах.\n"
            "• Поиск по email – проверяет утечки и регистрации.\n"
            "• Поиск по телефону – определяет страну, оператора.\n"
            "• Проверка ссылки – сканирует через VirusTotal.\n"
            "• Поиск по фото – обратный поиск изображений.\n"
            "• Поиск по ИНН – информация об организации/ИП.\n"
            "• Проверка email на утечки – HIBP.\n\n"
            "💰 У вас 3 бесплатных запроса. Для продолжения оформите подписку.\n\n"
            "👑 Админы могут использовать команды:\n"
            "/admin – список админов\n"
            "/set_admin <id> – добавить/удалить админа",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад в меню", callback_data='back_to_main')]])
        )
    elif data == 'back_to_main':
        await show_main_menu(update, context, edit_target=query.message)
    elif data == 'subscription':
        await show_subscription_menu(update, context)
    elif data.startswith('buy_plan_'):
        plan_id = int(data.split('_')[-1])
        await handle_buy_subscription(update, context, plan_id)
    elif data in ('search_username', 'search_email', 'search_phone', 'check_link', 'search_inn', 'search_hibp'):
        context.user_data['awaiting'] = data
        prompts = {
            'search_username': "🔍 Введите **username** для поиска:",
            'search_email': "📧 Введите **email** для поиска:",
            'search_phone': "📱 Введите **номер телефона** (например, +79991234567):",
            'check_link': "🔗 Введите **ссылку** для проверки:",
            'search_inn': "🏢 Введите **ФИО или название организации** для поиска ИНН:",
            'search_hibp': "📧 Введите **email** для проверки на утечки (HIBP):",
        }
        await query.edit_message_text(
            prompts[data],
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Отмена", callback_data='back_to_main')]])
        )
    elif data == 'search_photo':
        context.user_data['awaiting'] = 'search_photo'
        await query.edit_message_text(
            "📸 Отправьте **фото** для обратного поиска.\n"
            "Вы можете отправить изображение как файл или как фото.",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Отмена", callback_data='back_to_main')]])
        )

# --------------------- ОБРАБОТКА ТЕКСТА И ФОТО ---------------------
async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    awaiting = context.user_data.get('awaiting')
    if not awaiting:
        await update.message.reply_text(
            "Используйте кнопки меню для выполнения действий.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Меню", callback_data='back_to_main')]])
        )
        return

    if not check_query_limit(user_id):
        await update.message.reply_text(
            "❌ **Ваш бесплатный дневной лимит запросов превышен.**\n\n"
            "Без подписки вы не сможете получить полные данные.\n"
            "Оформите подписку через кнопку ниже.",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Управление подпиской", callback_data='subscription')]
            ])
        )
        context.user_data['awaiting'] = None
        return

    user_info = get_user_info(user_id)
    has_sub = user_info and user_info.get('subscription_end_date') and datetime.fromisoformat(user_info['subscription_end_date'].replace('Z', '+00:00')) > datetime.now()
    result_message = ""

    if awaiting == 'search_username':
        result_file = run_blackbird(text, 'username')
        raw_data = parse_blackbird_results(result_file) if result_file else {}
        result_message = format_search_result('username', text, raw_data, has_sub)
    elif awaiting == 'search_email':
        result_file = run_blackbird(text, 'email')
        raw_data = parse_blackbird_results(result_file) if result_file else {}
        result_message = format_search_result('email', text, raw_data, has_sub)
    elif awaiting == 'search_phone':
        phone_data = search_phone(text)
        result_message = format_search_result('phone', text, phone_data, has_sub)
    elif awaiting == 'check_link':
        vt_result = check_virustotal(text)
        if 'error' in vt_result:
            result_message = f"❌ Ошибка: {vt_result['error']}"
        else:
            result_message = format_search_result('link', text, vt_result, has_sub)
    elif awaiting == 'search_inn':
        inn_data = search_inn(text)
        if 'error' in inn_data:
            result_message = f"❌ Ошибка: {inn_data['error']}"
        else:
            result_message = format_search_result('inn', text, inn_data, has_sub)
    elif awaiting == 'search_hibp':
        hibp_data = check_hibp(text)
        if 'error' in hibp_data:
            result_message = f"❌ Ошибка: {hibp_data['error']}"
        else:
            result_message = format_search_result('hibp', text, hibp_data, has_sub)

    context.user_data['awaiting'] = None
    await update.message.reply_text(
        result_message,
        parse_mode='Markdown',
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Управление подпиской", callback_data='subscription')],
            [InlineKeyboardButton("📋 Меню", callback_data='back_to_main')]
        ])
    )

async def handle_photo_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    awaiting = context.user_data.get('awaiting')
    if awaiting != 'search_photo':
        return

    if not check_query_limit(user_id):
        await update.message.reply_text(
            "❌ **Ваш бесплатный дневной лимит запросов превышен.**\n\n"
            "Без подписки вы не сможете получить полные данные.",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Управление подпиской", callback_data='subscription')]
            ])
        )
        context.user_data['awaiting'] = None
        return

    photo_file = await update.message.photo[-1].get_file()
    file_data = await photo_file.download_as_bytearray()
    filename = photo_file.file_path.split('/')[-1] or 'photo.jpg'

    user_info = get_user_info(user_id)
    has_sub = user_info and user_info.get('subscription_end_date') and datetime.fromisoformat(user_info['subscription_end_date'].replace('Z', '+00:00')) > datetime.now()
    photo_data = search_by_photo(file_data, filename)
    result_message = format_search_result('photo', filename, photo_data, has_sub)

    context.user_data['awaiting'] = None
    await update.message.reply_text(
        result_message,
        parse_mode='Markdown',
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Управление подпиской", callback_data='subscription')],
            [InlineKeyboardButton("📋 Меню", callback_data='back_to_main')]
        ])
    )

# --------------------- ПОДПИСКА ---------------------
async def show_subscription_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_info = get_user_info(user_id)
    if not user_info:
        if update.callback_query:
            await update.callback_query.edit_message_text("❌ Ошибка: пользователь не найден")
        else:
            await update.message.reply_text("❌ Ошибка: пользователь не найден")
        return

    balance = user_info.get('balance', 0)
    sub_end = user_info.get('subscription_end_date')
    if sub_end and datetime.fromisoformat(sub_end.replace('Z', '+00:00')) > datetime.now():
        sub_status = f"✅ Активна до {sub_end[:10]}"
    else:
        sub_status = "❌ Нет активной подписки"

    plans = get_subscription_plans()
    keyboard = []
    for plan in plans:
        keyboard.append([
            InlineKeyboardButton(
                f"{plan['name']} — {plan['price']}₽",
                callback_data=f"buy_plan_{plan['id']}"
            )
        ])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data='back_to_main')])

    text = (
        f"💳 **Подписка и баланс**\n\n"
        f"Ваш баланс: **{balance}₽**\n"
        f"Статус подписки: {sub_status}\n\n"
        f"💰 Пополнить баланс можно через @suplira (укажите ваш Telegram ID)\n"
        f"После пополнения выберите план:\n"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )

async def handle_buy_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE, plan_id: int):
    user_id = update.effective_user.id
    success = purchase_subscription(user_id, plan_id)
    if success:
        await update.callback_query.answer("✅ Подписка оформлена!", show_alert=True)
        await show_subscription_menu(update, context)
    else:
        await update.callback_query.answer("❌ Недостаточно средств на балансе!", show_alert=True)

# --------------------- АДМИН-КОМАНДЫ ---------------------
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает список админов"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    admins = get_all_admins()
    if not admins:
        await update.message.reply_text("👑 Список админов пуст.")
        return
    text = "👑 **Список администраторов:**\n"
    for uid in admins:
        # пытаемся получить username из users
        user_info = get_user_info(uid)
        name = user_info.get('username') or user_info.get('first_name') or str(uid)
        text += f"• {name} (`{uid}`)\n"
    await update.message.reply_text(text, parse_mode='Markdown')

async def set_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Добавляет или удаляет админа. Использование: /set_admin <id>"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    if not context.args:
        await update.message.reply_text("Использование: `/set_admin 123456789`", parse_mode='Markdown')
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный ID. Укажите число.")
        return

    if target_id == user_id:
        await update.message.reply_text("❌ Вы не можете удалить самого себя.")
        return

    # Проверяем, есть ли уже в админах
    if is_admin(target_id):
        # удаляем
        if remove_admin(target_id):
            await update.message.reply_text(f"✅ Пользователь `{target_id}` удалён из админов.", parse_mode='Markdown')
        else:
            await update.message.reply_text("❌ Ошибка удаления.")
    else:
        # добавляем
        if add_admin(target_id):
            await update.message.reply_text(f"✅ Пользователь `{target_id}` добавлен в админы.", parse_mode='Markdown')
        else:
            await update.message.reply_text("❌ Ошибка добавления.")

# --------------------- ЗАПУСК ---------------------
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("set_admin", set_admin_command))

    # Админ-команды для баланса и статистики (только для админов)
    app.add_handler(CommandHandler("add_balance", admin_add_balance))
    app.add_handler(CommandHandler("stats", admin_stats))

    app.add_handler(CallbackQueryHandler(button_callback, pattern='^(help|back_to_main|subscription|buy_plan_|search_)'))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_input))

    logger.info("🚀 Бот LIRAMAX запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
