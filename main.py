import os
import json
import base64
import logging
import requests
from pathlib import Path

import msal
from openai import OpenAI
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    Application, MessageHandler, CommandHandler,
    CallbackQueryHandler, filters, ContextTypes,
)
from email_analyzer import analyze_email

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY      = os.getenv("OPENAI_API_KEY")
OPENAI_BASE_URL     = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
OUTLOOK_EMAIL       = os.getenv("OUTLOOK_EMAIL")
AZURE_TENANT_ID     = os.getenv("AZURE_TENANT_ID")
AZURE_CLIENT_ID     = os.getenv("AZURE_CLIENT_ID")
AZURE_CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET")
SENDER_NAME         = os.getenv("SENDER_NAME", "Florian")

# 新加坡：有 PDF 附件
TEMPLATE_SG   = Path(__file__).parent / "email_template.html"
ATTACHMENTS_SG = [
    Path(__file__).parent / "company-pi" / "Corporate Account Opening Application Form .pdf",
    Path(__file__).parent / "company-pi" / "Accredited Investor Declaration Form_Corporate.pdf",
]

# 香港：纯 HTML，无附件
TEMPLATE_HK   = Path(__file__).parent / "email_template_hk.html"


# ─── Microsoft Graph：获取 Token ─────────────────────────────────────────────

def get_graph_token() -> str:
    authority = f"https://login.microsoftonline.com/{AZURE_TENANT_ID}"
    app = msal.ConfidentialClientApplication(
        AZURE_CLIENT_ID,
        authority=authority,
        client_credential=AZURE_CLIENT_SECRET,
    )
    result = app.acquire_token_for_client(
        scopes=["https://graph.microsoft.com/.default"]
    )
    if "access_token" not in result:
        raise RuntimeError(f"获取 Graph token 失败: {result.get('error_description')}")
    return result["access_token"]


# ─── 发送邮件 ─────────────────────────────────────────────────────────────────

def send_onboarding_email(to_email: str, entity_name: str, region: str) -> None:
    subject_entity = entity_name if entity_name else "New Client"
    subject = f"HTS CORPORATE ONBOARDING – {subject_entity}"

    if region == "hk":
        html_body = TEMPLATE_HK.read_text(encoding="utf-8")
        attachments = []
    else:
        html_body = TEMPLATE_SG.read_text(encoding="utf-8")
        attachments = []
        for path in ATTACHMENTS_SG:
            with open(path, "rb") as f:
                content_b64 = base64.b64encode(f.read()).decode("utf-8")
            attachments.append({
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": path.name,
                "contentType": "application/pdf",
                "contentBytes": content_b64,
            })

    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": [{"emailAddress": {"address": to_email}}],
            "ccRecipients": [{"emailAddress": {"address": "james.yang@hashkey.com"}}],
            "attachments": attachments,
        },
        "saveToSentItems": True,
    }

    token = get_graph_token()
    url = f"https://graph.microsoft.com/v1.0/users/{OUTLOOK_EMAIL}/sendMail"
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )

    if resp.status_code != 202:
        raise RuntimeError(f"Graph API 发送失败 ({resp.status_code}): {resp.text}")

    logger.info(f"邮件已发送至 {to_email} [{region.upper()}]")


# ─── AI 识别截图 ──────────────────────────────────────────────────────────────

def extract_info_from_image(image_bytes: bytes) -> dict:
    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    if image_bytes[:3] == b'\xff\xd8\xff':
        mime = "image/jpeg"
    elif image_bytes[:8] == b'\x89PNG\r\n\x1a\n':
        mime = "image/png"
    elif image_bytes[:4] == b'RIFF' and image_bytes[8:12] == b'WEBP':
        mime = "image/webp"
    else:
        mime = "image/jpeg"

    response = client.chat.completions.create(
        model="anthropic/claude-sonnet-4.6",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
                {"type": "text", "text": (
                    "请从这张截图中提取以下信息，以JSON格式返回：\n"
                    "1. email: 客户邮箱地址\n"
                    "2. entity_name: 公司/实体名称（如果有）\n\n"
                    "只返回JSON，例如：\n"
                    '{"email": "client@example.com", "entity_name": "ABC Company Ltd"}\n'
                    "如果某项信息不存在，对应值设为空字符串。"
                )},
            ],
        }],
    )

    raw = response.choices[0].message.content.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


# ─── Telegram Bot ─────────────────────────────────────────────────────────────

async def _ask_region(message, email: str, entity_name: str) -> None:
    """识别完成后，询问发哪个地区的邮件"""
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🇸🇬 新加坡", callback_data="region_sg"),
        InlineKeyboardButton("🇭🇰 香港", callback_data="region_hk"),
        InlineKeyboardButton("❌ 取消", callback_data="cancel"),
    ]])
    await message.reply_text(
        f"识别结果：\n"
        f"📧 邮箱：{email}\n"
        f"🏢 公司：{entity_name}\n\n"
        f"请选择发送哪个地区的开户邮件：",
        reply_markup=keyboard,
    )


async def _send_confirmation(message, email: str, entity_name: str, region: str) -> None:
    region_label = "🇸🇬 新加坡" if region == "sg" else "🇭🇰 香港"
    attachment_note = "附件：Corporate Account Opening Form + AI Declaration Form\n" if region == "sg" else ""
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 确认发送", callback_data="confirm"),
        InlineKeyboardButton("❌ 取消", callback_data="cancel"),
    ]])
    await message.reply_text(
        f"发送确认：\n"
        f"📧 邮箱：{email}\n"
        f"🏢 公司：{entity_name}\n"
        f"🌏 地区：{region_label}\n"
        f"{attachment_note}\n"
        f"确认发送？",
        reply_markup=keyboard,
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["started"] = True
    await update.message.reply_text(
        "👋 HTS Onboarding Bot\n\n"
        "📨 邮件分析：\n"
        "• 直接粘贴邮件内容（文字），Bot 自动分析优先级、部门、摘要和待办\n\n"
        "📋 客户开户：\n"
        "• 发送客户截图，Bot 自动识别邮箱和公司名\n"
        "• 选择新加坡或香港站，确认后自动发出开户邮件\n\n"
        "指令：\n"
        "/start — 显示此帮助\n\n"
        "请发送截图或粘贴邮件内容开始。"
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message

    if not context.user_data.get("started"):
        await message.reply_text("请先发送 /start 启动 Bot。")
        return

    await message.reply_text("收到截图，正在识别客户信息...")

    photo = message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_bytes = bytes(await file.download_as_bytearray())

    try:
        info = extract_info_from_image(image_bytes)
        email = info.get("email", "").strip()
        entity_name = info.get("entity_name", "").strip()

        if not email:
            await message.reply_text("未能识别出邮箱地址，请检查截图是否清晰后重试。")
            return

        context.user_data["pending_email"] = email
        context.user_data["pending_entity"] = entity_name

        if not entity_name:
            context.user_data["waiting_for_entity"] = True
            await message.reply_text(
                f"识别结果：\n"
                f"📧 邮箱：{email}\n"
                f"🏢 公司：未能识别\n\n"
                f"请手动输入公司名称："
            )
            return

        await _ask_region(message, email, entity_name)

    except Exception as e:
        logger.error(f"处理失败: {e}", exc_info=True)
        await message.reply_text(f"❌ 处理失败：{e}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    text = message.text.strip()

    # 开户流程：等待用户手动输入公司名
    if context.user_data.get("waiting_for_entity"):
        context.user_data["pending_entity"] = text
        context.user_data["waiting_for_entity"] = False
        email = context.user_data.get("pending_email", "")
        await _ask_region(message, email, text)
        return

    # 邮件分析：用户粘贴邮件内容（50字以上视为邮件）
    if len(text) >= 50:
        thinking_msg = await message.reply_text("正在分析邮件，请稍候...")
        try:
            result = analyze_email(text)
            await thinking_msg.delete()
            await message.reply_text(result, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"邮件分析失败: {e}", exc_info=True)
            await thinking_msg.edit_text(f"分析失败：{e}")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        context.user_data.clear()
        await query.edit_message_text("已取消。")
        return

    # 选择地区
    if query.data in ("region_sg", "region_hk"):
        region = "sg" if query.data == "region_sg" else "hk"
        context.user_data["pending_region"] = region
        email = context.user_data.get("pending_email", "")
        entity_name = context.user_data.get("pending_entity", "")
        region_label = "🇸🇬 新加坡" if region == "sg" else "🇭🇰 香港"
        attachment_note = "附件：Corporate Account Opening Form + AI Declaration Form\n" if region == "sg" else ""
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ 确认发送", callback_data="confirm"),
            InlineKeyboardButton("❌ 取消", callback_data="cancel"),
        ]])
        await query.edit_message_text(
            f"发送确认：\n"
            f"📧 邮箱：{email}\n"
            f"🏢 公司：{entity_name}\n"
            f"🌏 地区：{region_label}\n"
            f"{attachment_note}\n"
            f"确认发送？",
            reply_markup=keyboard,
        )
        return

    # 确认发送
    if query.data == "confirm":
        email = context.user_data.get("pending_email")
        entity_name = context.user_data.get("pending_entity", "")
        region = context.user_data.get("pending_region", "sg")

        if not email:
            await query.edit_message_text("❌ 没有待发送的邮件，请重新发送截图。")
            return

        await query.edit_message_text(f"正在发送邮件给 {email}...")

        try:
            send_onboarding_email(email, entity_name, region)
            context.user_data.clear()
            region_label = "🇸🇬 新加坡" if region == "sg" else "🇭🇰 香港"
            await query.edit_message_text(
                f"✅ 邮件已发送！\n"
                f"收件人：{email}\n"
                f"地区：{region_label}\n"
                f"主题：HTS CORPORATE ONBOARDING – {entity_name or 'New Client'}"
            )
        except Exception as e:
            logger.error(f"发送失败: {e}", exc_info=True)
            await query.edit_message_text(f"❌ 发送失败：{e}")


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, (NetworkError, TimedOut)):
        logger.debug(f"网络波动（自动重连）: {context.error}")
    else:
        logger.error(f"未预期错误: {context.error}", exc_info=context.error)


def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_error_handler(handle_error)
    logger.info("Bot 已启动，等待截图...")
    app.run_polling()


if __name__ == "__main__":
    main()
