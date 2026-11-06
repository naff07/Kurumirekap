import os
import re
import io
import logging
import sqlite3
import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, getcontext

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)

getcontext().prec = 50

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

# ==================== KONFIGURASI ADMIN ====================
# GANTI ANGKA DI BAWAH INI DENGAN ID TELEGRAM KAMU
ADMIN_ID = 123456789 
# ==========================================================

MAX_LINES = 1000
MAX_CHAT_LINES = 300
MAX_MSG_LEN = 4000

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== DATABASE ====================
DB_FILE = "database.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS utang
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  user_id INTEGER, nama TEXT, nominal TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS pengingat
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  user_id INTEGER, chat_id INTEGER, pesan TEXT, jam TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY, 
                  username TEXT, first_name TEXT, joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

def catat_user(user):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
              (user.id, user.username or "", user.first_name or ""))
    conn.commit()
    conn.close()

# ==================== UTILITAS ANGKA ====================
def parse_decimal(s: str) -> Decimal:
    s = s.strip().replace(' ', '')
    if not s: raise ValueError("angka kosong")
    neg = s.startswith('-')
    if neg: s = s[1:]

    if ',' in s and '.' in s:
        if s.rfind(',') < s.rfind('.'): s = s.replace(',', '')
        else: s = s.replace('.', '').replace(',', '.')
    elif ',' in s:
        s = s.replace(',', '') if s.count(',') > 1 else s.replace(',', '.')
    elif '.' in s:
        if s.count('.') > 1: s = s.replace('.', '')

    if not s or s in ('.', ','): raise ValueError("angka kosong")
    try: d = Decimal(s)
    except InvalidOperation: raise ValueError(f"angka tidak valid: {s}")
    return -d if neg else d

def _group(int_str: str) -> str:
    if len(int_str) <= 3: return int_str
    parts = []
    while len(int_str) > 3:
        parts.insert(0, int_str[-3:])
        int_str = int_str[:-3]
    parts.insert(0, int_str)
    return '.'.join(parts)

def _fmt(d: Decimal) -> str:
    try: d = d.quantize(Decimal(10) ** -6, rounding=ROUND_HALF_UP)
    except InvalidOperation: return str(d)
    s = f"{d:f}"
    int_part, _, dec_part = s.partition('.')
    dec_part = dec_part.rstrip('0')
    neg = int_part.startswith('-')
    if neg: int_part = int_part[1:]
    int_part = _group(int_part or '0')
    if neg: int_part = '-' + int_part
    return f"{int_part},{dec_part}" if dec_part else int_part

def _esc(s: str) -> str:
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

# ==================== MESIN KALKULATOR & PARSING ====================
def hitung_ekspresi(expr_str: str) -> tuple:
    expr_str = expr_str.replace('?', '*').replace('x', '*').replace('X', '*').replace('?', '/').replace(':', '/')
    parts = re.findall(r'[\d.,]+|[\+\-\*/]', expr_str)
    if not parts: raise ValueError("ekspresi kosong")
    result = parse_decimal(parts[0])
    op_strs = []
    i = 1
    while i < len(parts):
        op = parts[i]
        if i + 1 >= len(parts): raise ValueError("ekspresi tidak lengkap")
        num = parse_decimal(parts[i+1])
        if op == '*': result *= num; op_strs.append('?')
        elif op == '/':
            if num == 0: raise ValueError("pembagian dengan nol")
            result /= num; op_strs.append('?')
        elif op == '+': result += num; op_strs.append('+')
        elif op == '-': result -= num; op_strs.append('-')
        i += 2
    label_parts = [parts[0]]
    for idx, op_str in enumerate(op_strs):
        label_parts.append(op_str); label_parts.append(parts[(idx * 2) + 2])
    return result, " ".join(label_parts)

def parse_line(line: str) -> dict:
    line = line.strip()
    if not line: raise ValueError("baris kosong")
    
    clean_line = line.replace('=', ' ').replace(':', ' ')

    # 1. PRIORITAS UTAMA: Ambil angka paling ujung kanan sebagai nilai
    m = re.match(r'^(.+?)\s+([\d.,]+)$', clean_line)
    if m:
        prefix_text = m.group(1).strip()
        value_str = m.group(2)
        
        # Cek apakah teks depannya ini sebenarnya format Range (misal: 01-20)
        m_range = re.match(r'^(.+?)\s+(\d+)-(\d+)$', prefix_text)
        if m_range:
            p, n1_s, n2_s = m_range.group(1).strip(), m_range.group(2), m_range.group(3)
            n1, n2 = int(n1_s), int(n2_s)
            if n2 < n1: raise ValueError("range terbalik")
            if n2 - n1 + 1 > MAX_LINES: raise ValueError("range terlalu panjang")
            return {'type': 'range', 'prefix': p, 'start': n1, 'end': n2, 'width': len(n1_s), 'value': parse_decimal(value_str)}
        
        # Jika bukan range, anggap ini Single Item (teks + nilai)
        return {'type': 'single', 'prefix': prefix_text, 'num': '', 'value': parse_decimal(value_str)}

    # 2. Cek Range Tanpa Nilai
    m = re.match(r'^(.+?)\s+(\d+)-(\d+)$', clean_line)
    if m:
        p, n1_s, n2_s = m.group(1).strip(), m.group(2), m.group(3)
        n1, n2 = int(n1_s), int(n2_s)
        if n2 < n1: raise ValueError("range terbalik")
        if n2 - n1 + 1 > MAX_LINES: raise ValueError("range terlalu panjang")
        return {'type': 'range', 'prefix': p, 'start': n1, 'end': n2, 'width': len(n1_s), 'value': None}

    # 3. Cek Ekspresi Matematika
    if re.search(r'[\d.,]+\s*[\+\-\*/\?\?\:xX]\s*[\d.,]+', line):
        val, label = hitung_ekspresi(line)
        return {'type': 'expr', 'value': val, 'label': label}

    # 4. Angka saja
    try: return {'type': 'bare', 'value': parse_decimal(line)}
    except ValueError: pass
    
    # 5. Teks tanpa angka
    return {'type': 'single', 'prefix': line, 'num': '', 'value': None}

def expand(p: dict):
    t = p['type']
    if t == 'single': return [(f"{p['prefix']}", p['value'])]
    if t == 'range': return [(f"{p['prefix']} {str(n).zfill(p['width'])}", p['value']) for n in range(p['start'], p['end'] + 1)]
    if t == 'expr': return [(f"{p['label']}", p['value'])]
    if t == 'bare': return [(None, p['value'])]
    return []

def render_items(items) -> str:
    lines = []
    for label, val in items:
        if label is None: lines.append(_fmt(val) if val is not None else "-")
        else: 
            if val is not None: lines.append(f"{label} {_fmt(val)}")
            else: lines.append(f"{label}")
    return "\n".join(lines)

def build_summary(items, errors) -> str:
    values = [v for _, v in items if v is not None]
    if not values:
        out = ["????????????????", "? <b>Ringkasan</b>", f"Total baris teks: <b>{len(items)}</b>"]
        if errors: out.append(f"?? {len(errors)} baris gagal.")
        return "\n".join(out)

    total = sum(values, Decimal(0))
    n = len(values)
    avg = total / n if n else Decimal(0)
    mn, mx = min(values), max(values)
    zeros = sum(1 for v in values if v == 0)
    
    str_values = [_fmt(v) for v in values]
    if len(str_values) <= 10: deret_str = " + ".join(str_values)
    else: deret_str = " + ".join(str_values[:5]) + " + ... + " + " + ".join(str_values[-3:])

    out = [
        "????????????????", "? <b>Ringkasan Perhitungan</b>",
        f"Baris dihitung: <b>{n}</b>", f"Total: <b>{_esc(_fmt(total))}</b>",
        f"Rata-rata: <b>{_esc(_fmt(avg))}</b>", f"Min: <b>{_esc(_fmt(mn))}</b>", f"Max: <b>{_esc(_fmt(mx))}</b>\n",
        f"<pre>{_esc(deret_str)} = {_esc(_fmt(total))}</pre>"
    ]
    if zeros: out.append(f"?? Baris bernilai 0: <b>{zeros}</b>")
    if errors: out.append(f"?? {len(errors)} baris gagal.")
    return "\n".join(out)

# ==================== BACKGROUND TASK (ALARM) ====================
async def cek_pengingat(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.datetime.now().strftime("%H:%M")
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, chat_id, pesan FROM pengingat WHERE jam = ?", (now,))
    due_reminders = c.fetchall()
    
    for rid, chat_id, pesan in due_reminders:
        try:
            await context.bot.send_message(chat_id=chat_id, text=f"? <b>PENGINGAT HARIAN!</b>\n\n{pesan}", parse_mode='HTML')
        except Exception as e:
            logger.error(f"Gagal kirim pengingat: {e}")
            
    conn.close()

# ==================== HANDLERS ====================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        msg = update.message
        if not msg or not msg.text: return

        catat_user(msg.from_user)

        # Abaikan pesan di grup (Bot hanya jalan di Private Chat)
        if update.effective_chat.type in ('group', 'supergroup'):
            return

        raw = msg.text.strip()
        lines = raw.split('\n')

        utang_items = []
        is_utang_list = True
        for ln in lines:
            if not ln.strip(): continue
            m_utang = re.match(r'^@([a-zA-Z0-9_]+)\s+([\d.,]+\s*[a-zA-Z]*)', ln.strip())
            if m_utang:
                utang_items.append((m_utang.group(1), m_utang.group(2)))
            else:
                is_utang_list = False
                break

        if is_utang_list and utang_items:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            for nama, nominal in utang_items:
                c.execute("INSERT INTO utang (user_id, nama, nominal) VALUES (?, ?, ?)", (msg.from_user.id, nama, nominal))
            conn.commit()
            conn.close()
            return await msg.reply_text(f"? Berhasil mencatat <b>{len(utang_items)}</b> utang/tagihan.\nKetik /list_utang untuk melihatnya.", parse_mode='HTML')

        items, errors = [], []
        for i, ln in enumerate(lines, 1):
            if not ln.strip(): continue
            try: items.extend(expand(parse_line(ln)))
            except ValueError as e: errors.append(f"Baris {i}: {e}")

        if not items:
            detail = "\n".join(errors[:5]) if errors else "Tidak ada data."
            return await msg.reply_text(f"? Tidak ada data valid.\n\n{_esc(detail)}", parse_mode='HTML')

        summary = build_summary(items, errors)
        if len(items) > MAX_CHAT_LINES:
            buf = io.BytesIO(render_items(items).encode('utf-8'))
            buf.name = "rekap.txt"
            return await msg.reply_document(document=buf, caption=summary, parse_mode='HTML')

        watermark = f"\n\n<i>Created by: @{context.bot.username}</i>"
        full = f"<pre>{_esc(render_items(items))}</pre>\n\n{summary}{watermark}"
        
        if len(full) > MAX_MSG_LEN:
            buf = io.BytesIO(render_items(items).encode('utf-8'))
            buf.name = "rekap.txt"
            return await msg.reply_document(document=buf, caption="? Hasil terlalu panjang, dikirim sebagai file.")
        await msg.reply_text(full, parse_mode='HTML')

    except Exception as e:
        logger.exception("handler error")
        try: await update.message.reply_text(f"? Error: {_esc(str(e))[:200]}")
        except Exception: pass

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return await update.message.reply_text("? Maaf, perintah ini hanya untuk Admin/Owner bot.")

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM utang")
    total_utang = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM pengingat")
    total_alarm = c.fetchone()[0]
    c.execute("SELECT first_name, username FROM users ORDER BY joined_at DESC LIMIT 5")
    recent_users = c.fetchall()
    conn.close()

    text = (
        "? <b>STATISTIK BOT (ADMIN)</b>\n\n"
        f"? Total Pengguna: <b>{total_users}</b> orang\n"
        f"? Total Catatan Utang: <b>{total_utang}</b>\n"
        f"? Total Alarm Aktif: <b>{total_alarm}</b>\n\n"
        "<b>5 Pengguna Terbaru:</b>\n"
    )
    for name, uname in recent_users:
        text += f"• {name} (@{uname})\n" if uname else f"• {name}\n"

    await update.message.reply_text(text, parse_mode='HTML')

async def list_utang_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT nama, nominal FROM utang WHERE user_id = ?", (update.effective_user.id,))
    rows = c.fetchall()
    conn.close()

    if not rows:
        return await update.message.reply_text("? Belum ada catatan utang.")

    text = "? <b>Daftar Utang/Tagihan Kamu:</b>\n\n"
    for nama, nominal in rows:
        text += f"• <b>@{nama}</b> : {nominal}\n"
    
    text += "\n<i>(Ketik /hapus_utang untuk membersihkan daftar)</i>"
    await update.message.reply_text(text, parse_mode='HTML')

async def hapus_utang_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM utang WHERE user_id = ?", (update.effective_user.id,))
    conn.commit()
    conn.close()
    await update.message.reply_text("?? Semua catatan utang berhasil dihapus.")

async def reminder_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if len(context.args) < 2:
            return await update.message.reply_text(
                "?? <b>Format Salah!</b>\n\n"
                "Gunakan format: <code>/reminder HH:MM pesan</code>\n\n"
                "Contoh:\n<code>/reminder 20:00 Tagih utang @anu1</code>",
                parse_mode='HTML')

        jam_str = context.args[0].replace(' ', '')
        pesan = " ".join(context.args[1:])

        if re.match(r'^\d{1,2}:\d{2}$', jam_str):
            parts = jam_str.split(':')
            jam_str = f"{int(parts[0]):02d}:{parts[1]}"

        try:
            datetime.datetime.strptime(jam_str, "%H:%M")
        except ValueError:
            return await update.message.reply_text("? Format jam salah. Gunakan format 24 jam (contoh: 08:30 atau 20:00).")

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT INTO pengingat (user_id, chat_id, pesan, jam) VALUES (?, ?, ?, ?)",
                  (update.effective_user.id, update.effective_chat.id, pesan, jam_str))
        conn.commit()
        conn.close()

        await update.message.reply_text(f"? <b>Alarm Harian diset!</b>\n\n? Jam: {jam_str} (Setiap hari)\n? Pesan: {pesan}", parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"? Error internal: {str(e)}")

async def list_reminder_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, jam, pesan FROM pengingat WHERE user_id = ?", (update.effective_user.id,))
    rows = c.fetchall()
    conn.close()

    if not rows:
        return await update.message.reply_text("? Belum ada alarm harian yang aktif.")

    text = "? <b>Daftar Alarm Harian Kamu:</b>\n\n"
    for rid, jam, pesan in rows:
        text += f"• <code>{jam}</code> - {pesan}\n"
    
    text += "\n<i>(Ketik /hapus_reminder untuk mematikan semua alarm)</i>"
    await update.message.reply_text(text, parse_mode='HTML')

async def hapus_reminder_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM pengingat WHERE user_id = ?", (update.effective_user.id,))
    conn.commit()
    conn.close()
    await update.message.reply_text("?? Semua alarm harian berhasil dimatikan.")

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    catat_user(update.effective_user)
    await update.message.reply_text(
        "? <b>Bot Asisten & Rekapan Otomatis</b>\n\n"
        "<b>? Fitur Kalkulator:</b>\n"
        "• <code>shopee 01 58</code> (Hitung nilai)\n"
        "• <code>1311*0.55/7.2</code> (Matematika berantai)\n\n"
        "<b>? Fitur Catat Utang (Tagihan):</b>\n"
        "Ketik langsung (bisa multi-baris):\n"
        "<code>@budi 2000000</code>\n"
        "<code>@ani 100usdt</code>\n"
        "? /list_utang (Lihat daftar)\n"
        "? /hapus_utang (Hapus daftar)\n\n"
        "<b>? Fitur Alarm Harian:</b>\n"
        "<code>/reminder HH:MM pesan</code>\n"
        "Contoh: <code>/reminder 20:00 Tagih Ani</code>\n"
        "? /list_reminder (Lihat alarm)\n"
        "? /hapus_reminder (Matikan alarm)",
        parse_mode='HTML')

async def error_handler(update, context):
    logger.error(f"update error: {context.error}")

def main():
    if not BOT_TOKEN:
        print("? Environment variable BOT_TOKEN belum diset.")
        return

    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", start_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("list_utang", list_utang_cmd))
    app.add_handler(CommandHandler("hapus_utang", hapus_utang_cmd))
    app.add_handler(CommandHandler("reminder", reminder_cmd))
    app.add_handler(CommandHandler("list_reminder", list_reminder_cmd))
    app.add_handler(CommandHandler("hapus_reminder", hapus_reminder_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    job_queue = app.job_queue
    job_queue.run_repeating(cek_pengingat, interval=30, first=5)

    logger.info("Bot berjalan dengan fitur Database & Alarm Harian...")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()