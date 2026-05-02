#!/usr/bin/env python3

import os
import sys
import time
import logging
import random
import re
import requests
import undetected_chromedriver as uc
from datetime import datetime, timezone, timedelta
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import TimeoutException, WebDriverException
from dotenv import load_dotenv

load_dotenv()

# ===================== 配置日志 =====================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ===================== 全局配置 =====================
HEADLESS = os.getenv('HEADLESS', 'true').lower() == 'true'
PAUSE_BETWEEN_ACCOUNTS_MS = int(os.getenv('PAUSE_BETWEEN_ACCOUNTS_MS', '10000'))
TELEGRAM_BOT_TOKEN = os.getenv('BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('CHAT_ID', '')
ACCOUNTS_ENV = os.getenv('ACCOUNTS', '')
PROXY_SERVER = os.getenv('HTTP_PROXY', '')

# ===================== 工具函数 =====================
def rand_int(min_val, max_val):
    return random.randint(min_val, max_val)

def sleep(ms):
    time.sleep(ms / 1000)

def human_delay():
    delay = 7000 + random.random() * 5000
    sleep(delay)

def human_type(driver, selector_type, selector_value, text):
    try:
        element = WebDriverWait(driver, 15).until(EC.visibility_of_element_located((selector_type, selector_value)))
        element.clear()
        for char in text:
            element.send_keys(char)
            sleep(rand_int(50, 150))
        return True
    except Exception as e:
        logger.warning(f"打字失败: {e}")
        return False


STATUS_RENEWED = "renewed"
STATUS_NOT_DUE = "not_due"
STATUS_FAILED = "failed"

NOT_DUE_PATTERNS = [
    "you can't renew your server yet",
    "you cannot renew your server yet",
    "you will be able to as of",
]

def normalize_text(text):
    return re.sub(r"\s+", " ", (text or "")).strip()

def is_not_due_text(text):
    lowered = normalize_text(text).lower()
    return any(pattern in lowered for pattern in NOT_DUE_PATTERNS)

def extract_not_due_message(text):
    clean = normalize_text(text)
    if not clean:
        return "还没到可续期时间"
    m = re.search(r"You can(?:'|’)t renew your server yet\.\s*You will be able to as of\s*[^.]+\.?,?\s*\(in\s*\d+\s*day\(s\)\)\.?,?", clean, re.I)
    if m:
        return m.group(0).strip()
    m = re.search(r"You can(?:'|’)t renew your server yet[^\n]*", clean, re.I)
    if m:
        return m.group(0).strip()
    return clean[:300]

# ===================== Telegram 通知 =====================
def send_telegram(message, screenshot_path=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    tz_offset = timezone(timedelta(hours=8))
    time_str = datetime.now(tz_offset).strftime("%Y-%m-%d %H:%M:%S") + " HKT"
    full_message = f"🎉 Katabump 续期通知\n\n续期时间：{time_str}\n\n{message}"
    try:
        if screenshot_path and os.path.exists(screenshot_path):
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
            with open(screenshot_path, 'rb') as photo:
                requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "caption": full_message}, files={'photo': photo}, timeout=20)
        else:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": full_message}, timeout=10)
        logger.info("✅ Telegram 通知发送成功")
    except Exception as e:
        logger.warning(f"⚠️ Telegram 发送失败: {e}")

# ===================== Katabump 核心续期类 =====================
class KatabumpAutoRenew:
    def __init__(self, user, password):
        self.user = user
        self.password = password
        self.driver = None
        self.screenshot_path = None
        self.masked_user = self.mask_email()

    def mask_email(self):
        try:
            if "@" in self.user:
                prefix, domain = self.user.split('@')
                if len(prefix) <= 2:
                    return f"{prefix[0]}***@{domain}"
                return f"{prefix[0]}***{prefix[-1]}@{domain}"
            return f"{self.user[0]}***{self.user[-1]}" if len(self.user) > 2 else self.user
        except:
            return "UnknownUser"

    def setup_driver(self):
        chrome_options = Options()
        if HEADLESS: chrome_options.add_argument('--headless')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        if PROXY_SERVER:
            chrome_options.add_argument(f'--proxy-server={PROXY_SERVER}')
        v_env = os.getenv('CHROME_VERSION', '')
        v_main = int(v_env) if v_env.isdigit() else None
        logger.info(f"🛠️ 驱动初始化 - 指定大版本: {v_main or '自动探测'}")
        try:
            self.driver = uc.Chrome(options=chrome_options, headless=HEADLESS, version_main=v_main, use_subprocess=True)
        except Exception as e:
            logger.warning(f"⚠️ 强制版本启动失败，尝试降级启动: {e}")
            self.driver = uc.Chrome(options=chrome_options, headless=HEADLESS)
        self.driver.set_window_size(1280, 720)

    def _handle_turnstile(self, context="", required=False, timeout=8):
        """处理 Cloudflare Turnstile。required=False 时，没有验证框不算失败。"""
        try:
            container = WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located((By.CLASS_NAME, "cf-turnstile"))
            )
        except TimeoutException:
            if required:
                logger.error(f"❌ {self.masked_user} - [{context}] 未找到必需的验证框")
                return False
            logger.info(f"ℹ️ {self.masked_user} - [{context}] 未发现验证框，跳过验证步骤")
            return True
        except Exception as e:
            logger.warning(f"⚠️ {self.masked_user} - [{context}] 查找验证框异常: {e}")
            return not required

        try:
            size = container.size
            base_offset_x = -(size['width'] / 2) + (size['width'] * 0.12)
            rand_x = base_offset_x + random.uniform(-5, 5)
            rand_y = random.uniform(-5, 5)

            actions = ActionChains(self.driver)
            actions.move_to_element(container)
            actions.pause(random.uniform(0.5, 0.8))
            actions.move_to_element_with_offset(container, rand_x, rand_y)
            actions.click_and_hold()
            actions.pause(random.uniform(0.1, 0.25))
            actions.release()
            actions.perform()
            
            logger.info(f"🖱️ {self.masked_user} - [{context}] 执行偏移点击...")
            
            for _ in range(15):
                token = self.driver.execute_script(
                    """const el = document.querySelector("input[name='cf-turnstile-response']"); return el ? el.value : '';"""
                )
                if token and len(token) > 20:
                    logger.info(f"✅ {self.masked_user} - [{context}] 验证已通过 (Token Ready)")
                    sleep(1500 + random.random() * 1000)
                    return True
                sleep(1000)
            logger.warning(f"⚠️ {self.masked_user} - [{context}] 验证框存在，但未拿到 Token")
            return False
        except Exception as e:
            logger.error(f"❌ {self.masked_user} - [{context}] 验证交互失败: {e}")
            return False

    def _page_text(self):
        try:
            return normalize_text(self.driver.find_element(By.TAG_NAME, "body").text)
        except Exception:
            return ""

    def _renew_modal_text(self):
        selectors = ["#renew-modal", ".modal.show", ".modal", "body"]
        for selector in selectors:
            try:
                elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                for element in elements:
                    if element.is_displayed():
                        text = normalize_text(element.text)
                        if text:
                            return text
            except Exception:
                continue
        return self._page_text()

    def _visible_alert_texts(self):
        texts = []
        selectors = [".alert-danger", ".alert-warning", ".alert", "[role='alert']"]
        for selector in selectors:
            try:
                for element in self.driver.find_elements(By.CSS_SELECTOR, selector):
                    if element.is_displayed():
                        text = normalize_text(element.text.replace('×', ''))
                        if text and text not in texts:
                            texts.append(text)
            except Exception:
                continue
        return texts

    def _has_altcha(self):
        try:
            return bool(self.driver.execute_script("""
                return !!document.querySelector('altcha-widget') ||
                       document.body.innerText.includes('ALTCHA') ||
                       document.body.innerText.includes("I'm not a robot");
            """))
        except Exception:
            return 'ALTCHA' in self._page_text()

    def _handle_altcha(self, context="", timeout=60):
        """处理 ALTCHA 验证：点击 I'm not a robot，并等待本地 PoW payload 生成。"""
        if not self._has_altcha():
            logger.info(f"ℹ️ {self.masked_user} - [{context}] 未发现 ALTCHA，跳过 ALTCHA 步骤")
            return True
        logger.info(f"🧩 {self.masked_user} - [{context}] 检测到 ALTCHA，尝试点击 I'm not a robot...")

        clicked = False
        try:
            clicked = bool(self.driver.execute_script("""
                function visible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                }
                const widgets = Array.from(document.querySelectorAll('altcha-widget'));
                for (const w of widgets) {
                    const root = w.shadowRoot || w;
                    const candidates = [
                        root.querySelector('input[type="checkbox"]'),
                        root.querySelector('label'),
                        root.querySelector('button'),
                        root.querySelector('[role="checkbox"]')
                    ].filter(Boolean);
                    for (const el of candidates) {
                        try { el.scrollIntoView({block: 'center'}); } catch (e) {}
                        try { el.click(); return true; } catch (e) {}
                    }
                }
                const inputs = Array.from(document.querySelectorAll('input[type="checkbox"]'));
                for (const el of inputs) {
                    if (visible(el)) {
                        try { el.scrollIntoView({block: 'center'}); } catch (e) {}
                        try { el.click(); return true; } catch (e) {}
                    }
                }
                return false;
            """))
        except Exception as e:
            logger.warning(f"⚠️ {self.masked_user} - [{context}] ALTCHA JS 点击失败: {e}")

        if not clicked:
            try:
                checkbox = WebDriverWait(self.driver, 5).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, "input[type='checkbox']"))
                )
                self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", checkbox)
                checkbox.click()
                clicked = True
            except Exception:
                try:
                    label = WebDriverWait(self.driver, 5).until(
                        EC.element_to_be_clickable((By.XPATH, "//*[contains(., \"I'm not a robot\")]"))
                    )
                    self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", label)
                    label.click()
                    clicked = True
                except Exception as e:
                    logger.error(f"❌ {self.masked_user} - [{context}] ALTCHA 点击失败: {e}")
                    return False

        logger.info(f"🖱️ {self.masked_user} - [{context}] 已点击 ALTCHA，等待验证完成...")
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                state_payload = self.driver.execute_script("""
                    const w = document.querySelector('altcha-widget');
                    const state = w ? (w.getAttribute('state') || w.state || '') : '';
                    const payload = document.querySelector("input[name='altcha']")?.value || '';
                    const text = document.body.innerText || '';
                    return {state, payload, text};
                """)
                state = str(state_payload.get('state') or '').lower()
                payload = state_payload.get('payload') or ''
                text = state_payload.get('text') or ''
                if payload and len(payload) > 20:
                    logger.info(f"✅ {self.masked_user} - [{context}] ALTCHA 验证已通过 (Payload Ready)")
                    return True
                if state == 'verified':
                    logger.info(f"✅ {self.masked_user} - [{context}] ALTCHA 验证已通过 (State Verified)")
                    return True
                if is_not_due_text(text):
                    logger.info(f"⏳ {self.masked_user} - [{context}] ALTCHA 等待期间检测到未到时间提示")
                    return True
            except Exception:
                pass
            sleep(1000)
        logger.warning(f"⚠️ {self.masked_user} - [{context}] ALTCHA 等待超时，未检测到 payload")
        return False

    def process(self):
        logger.info(f"🚀 开始登录账号: {self.masked_user}")
        self.driver.get("https://dashboard.katabump.com/auth/login")
        sleep(5000 + random.random() * 2000)

        # --- 第一步：输入用户名 ---
        logger.info(f"📝 {self.masked_user} - 填写用户名/邮箱...")
        if not human_type(self.driver, By.CSS_SELECTOR, "input#email", self.user):
            raise Exception("未找到用户名输入框")
        sleep(2000 + random.random() * 1000)

        # --- 第二步：输入密码 ---
        logger.info(f"🔒 {self.masked_user} - 填写密码...")
        if not human_type(self.driver, By.CSS_SELECTOR, "input#password", self.password):
            raise Exception("未找到密码输入框")
        sleep(2000 + random.random() * 1000)

        # --- 登录页 CF 验证 ---
        self._handle_turnstile("Login Auth", required=False, timeout=8)

        logger.info(f"📤 {self.masked_user} - 点击“Login”提交登录...")
        self.driver.find_element(By.CSS_SELECTOR, 'button[type="submit"]').click()
        human_delay()

        # --- 第三步： Manage Server ---
        logger.info(f"🎯 {self.masked_user} - 进入服务器详情页...")
        manage_btn = WebDriverWait(self.driver, 30).until(
            EC.element_to_be_clickable((By.XPATH, "//a[contains(text(), 'See')]"))
        )
        self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", manage_btn)
        sleep(1000 + random.random() * 1000)
        self.driver.execute_script("arguments[0].click();", manage_btn)
        human_delay()

        # --- 第四步： Renew Server ---
        logger.info(f"🔄 {self.masked_user} - 准备续期流程...")
        initial_expiry = ""
        try:
            initial_expiry_element = WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.XPATH, "//div[contains(text(), 'Expiry')]/following-sibling::div"))
            )
            initial_expiry = initial_expiry_element.text.strip()
            logger.info(f"⌛ {self.masked_user} - 当前到期时间: {initial_expiry}")
        except Exception:
            logger.warning(f"⚠️ {self.masked_user} - 无法读取初始时间")

        try:
            renew_trigger = WebDriverWait(self.driver, 15).until(
                EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Renew')]"))
            )
            self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", renew_trigger)
            self.driver.execute_script("arguments[0].click();", renew_trigger)
            logger.info(f"📑 {self.masked_user} - 已打开 Renew 弹窗")
        except Exception as e:
            raise Exception(f"无法打开弹窗: {e}")
        sleep(2000 + random.random() * 1000)

        # --- 续期弹窗状态判断：先看文案，避免“未到时间”被误报成验证失败 ---
        modal_text = self._renew_modal_text()
        if modal_text:
            logger.info(f"🧾 {self.masked_user} - Renew 弹窗文本: {modal_text[:500]}")
        if is_not_due_text(modal_text):
            msg = extract_not_due_message(modal_text)
            logger.info(f"⏳ {self.masked_user} - 未到可续期时间: {msg}")
            return STATUS_NOT_DUE, f"⏳ {self.masked_user}\n未到可续期时间：{msg}\n当前到期时间：{initial_expiry or 'Unknown'}"

        # --- 续期弹窗验证：Katabump 当前使用 ALTCHA；如果没有 ALTCHA，再兼容旧的 Turnstile ---
        if self._has_altcha():
            if not self._handle_altcha("Renew Modal", timeout=70):
                modal_text = self._renew_modal_text()
                if is_not_due_text(modal_text):
                    msg = extract_not_due_message(modal_text)
                    logger.info(f"⏳ {self.masked_user} - 未到可续期时间: {msg}")
                    return STATUS_NOT_DUE, f"⏳ {self.masked_user}\n未到可续期时间：{msg}\n当前到期时间：{initial_expiry or 'Unknown'}"
                return STATUS_FAILED, f"❌ {self.masked_user}\nRenew 弹窗 ALTCHA 验证未通过，未继续点击最终 Renew。"
        elif not self._handle_turnstile("Renew Modal", required=False, timeout=5):
            modal_text = self._renew_modal_text()
            if is_not_due_text(modal_text):
                msg = extract_not_due_message(modal_text)
                logger.info(f"⏳ {self.masked_user} - 未到可续期时间: {msg}")
                return STATUS_NOT_DUE, f"⏳ {self.masked_user}\n未到可续期时间：{msg}\n当前到期时间：{initial_expiry or 'Unknown'}"
            return STATUS_FAILED, f"❌ {self.masked_user}\nRenew 弹窗验证未通过，未继续点击最终 Renew。"

        modal_text = self._renew_modal_text()
        if is_not_due_text(modal_text):
            msg = extract_not_due_message(modal_text)
            logger.info(f"⏳ {self.masked_user} - 未到可续期时间: {msg}")
            return STATUS_NOT_DUE, f"⏳ {self.masked_user}\n未到可续期时间：{msg}\n当前到期时间：{initial_expiry or 'Unknown'}"

        # --- 最终 Renew 按钮 ---
        try:
            confirm_btn_xpath = "//div[@id='renew-modal']//button[@type='submit' and contains(text(), 'Renew')]"
            confirm_btn = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, confirm_btn_xpath))
            )
            logger.info(f"🚀 {self.masked_user} - 点击最终 Renew 按钮...")
            self.driver.execute_script("arguments[0].click();", confirm_btn)
        except Exception as e:
            raise Exception(f"弹窗内提交失败: {e}")
            
        logger.info(f"⏳ {self.masked_user} - 等待数据更新...")
        sleep(7000 + random.random() * 2000)
        post_click_text = self._page_text()
        if post_click_text:
            logger.info(f"🧾 {self.masked_user} - 点击 Renew 后页面文本摘要: {post_click_text[:700]}")

        # 结果核验
        try:
            alert_texts = self._visible_alert_texts()
            combined_alerts = " | ".join(alert_texts)
            page_text = post_click_text or self._page_text()
            if is_not_due_text(combined_alerts) or is_not_due_text(page_text):
                msg = extract_not_due_message(combined_alerts or page_text)
                logger.info(f"⏳ {self.masked_user} - 未到可续期时间: {msg}")
                return STATUS_NOT_DUE, f"⏳ {self.masked_user}\n未到可续期时间：{msg}\n当前到期时间：{initial_expiry or 'Unknown'}"
            if alert_texts:
                alertmsg = combined_alerts
                logger.warning(f"⚠️ {self.masked_user} - 续期失败: {alertmsg}")
                return STATUS_FAILED, f"❌ {self.masked_user}\n续期失败：{alertmsg}"
            
            final_expiry_element = self.driver.find_element(By.XPATH, "//div[contains(text(), 'Expiry')]/following-sibling::div")
            final_expiry = final_expiry_element.text.strip()
            logger.info(f"✅ {self.masked_user} - 续期后到期时间: {final_expiry}")

            if final_expiry != initial_expiry and len(final_expiry) > 0:
                return STATUS_RENEWED, f"✅ {self.masked_user}\n🎉 续期成功：{initial_expiry or 'Unknown'} → {final_expiry}"
            else:
                return STATUS_FAILED, f"❌ {self.masked_user}\n续期后到期时间未变化：{initial_expiry or 'Unknown'} → {final_expiry or 'Unknown'}。未检测到明确的“未到时间”提示。"
        except Exception as e:
            return STATUS_FAILED, f"❌ {self.masked_user}\n验证结果出错: {e}"

    def run(self):
        """引入重试机制的核心运行逻辑"""
        max_retries = 3
        last_error = ""
        
        for attempt in range(max_retries):
            try:
                if not self.driver:
                    self.setup_driver()
                
                if attempt > 0:
                    logger.info(f"🔄 {self.masked_user} - 正在进行第 {attempt + 1} 次尝试...")
                    self.driver.refresh()
                    sleep(5000 + random.random() * 3000)

                status, message = self.process()
                
                if status in (STATUS_RENEWED, STATUS_NOT_DUE):
                    return status, message
                else:
                    last_error = message
                    break
                    
            except Exception as e:
                last_error = f"异常：{str(e)[:50]}"
                logger.error(f"❌ {self.masked_user} 第 {attempt + 1} 次执行出错: {e}")
                
            if attempt < max_retries - 1:
                sleep(5000 + random.random() * 5000)
        
        # 最终失败处理
        self.screenshot_path = f"error-{self.user.split('@')[0]}.png"
        if self.driver:
            self.driver.save_screenshot(self.screenshot_path)
        return STATUS_FAILED, f"❌ {self.masked_user} 历经 {max_retries} 次尝试仍失败: {last_error}"

# ===================== 主逻辑管理 =====================
class MultiManager:
    def __init__(self):
        raw_accs = re.split(r'[,;]', ACCOUNTS_ENV)
        self.accounts = []
        for a in raw_accs:
            if ':' in a:
                u, p = a.split(':', 1)
                self.accounts.append({'user': u.strip(), 'pass': p.strip()})

    def run_all(self):
        total = len(self.accounts)
        logger.info(f"🔍 发现 {total} 个账号需要处理")
        results = []
        last_screenshot = None
        renewed_count = 0
        not_due_count = 0
        failed_count = 0

        for i, acc in enumerate(self.accounts):
            logger.info(f"\n📋 处理第 {i+1}/{total} 个账号")
            bot = KatabumpAutoRenew(acc['user'], acc['pass'])
            status, msg = bot.run()
            results.append({'message': msg, 'status': status})
            if status == STATUS_RENEWED:
                renewed_count += 1
            elif status == STATUS_NOT_DUE:
                not_due_count += 1
            else:
                failed_count += 1
            if bot.screenshot_path: last_screenshot = bot.screenshot_path

            if i < total - 1:
                wait_time = PAUSE_BETWEEN_ACCOUNTS_MS + random.random() * 5000
                logger.info(f"⏳ 账号间歇期：等待 {round(wait_time/1000)} 秒...")
                sleep(wait_time)

        summary = (
            f"📊 Katabump 处理汇总\n"
            f"🎉 续期成功：{renewed_count}/{total}\n"
            f"⏳ 未到时间：{not_due_count}/{total}\n"
            f"❌ 真实失败：{failed_count}/{total}\n\n"
        )
        summary += "\n\n".join([r['message'] for r in results])
        logger.info("\n" + summary)
        send_telegram(summary, last_screenshot)

        if last_screenshot and os.path.exists(last_screenshot):
            import glob
            for f in glob.glob("error-*.png"): os.remove(f)
        if failed_count:
            logger.error(f"\n❌ 处理完成，但有 {failed_count} 个账号真实失败。")
            return 1
        logger.info("\n✅ 所有账号处理完成，没有真实失败。")
        return 0

if __name__ == "__main__":
    if not ACCOUNTS_ENV:
        logger.error("❌ 未配置账号")
        sys.exit(1)
    sys.exit(MultiManager().run_all())
