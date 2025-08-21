from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
import threading
import time
import os
import json
from datetime import datetime, timedelta
import firebase_admin
from firebase_admin import credentials, firestore

# Optional: Selenium-based booking
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from login import click_login_button, is_logged_in


class TextIn(BaseModel):
    text: str


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# === FIREBASE SETUP (supports env var for Render) ===
FIREBASE_CRED_PATH = os.path.join(os.path.dirname(__file__), "serviceAccountKey.json")

if not firebase_admin._apps:
    cred_env = os.getenv("FIREBASE_CRED_JSON")
    try:
        if cred_env:
            cred_dict = json.loads(cred_env)
            cred = credentials.Certificate(cred_dict)
        else:
            cred = credentials.Certificate(FIREBASE_CRED_PATH)
        firebase_admin.initialize_app(cred)
    except Exception:
        # Fallback to file if env failed
        cred = credentials.Certificate(FIREBASE_CRED_PATH)
        firebase_admin.initialize_app(cred)
db = firestore.client()

# === COOKIE FUNCTIONS (timestamped with 24h TTL) ===
def save_cookies_to_firebase(user_id, driver):
    try:
        cookies = driver.get_cookies()
        timestamp = datetime.utcnow().isoformat()
        db.collection("uber_cookies").document(user_id).set({
            "cookies": cookies,
            "timestamp": timestamp,
        })
        print(f"✅ Cookies saved for {user_id} at {timestamp}")
    except Exception as e:
        print(f"⚠️ Failed to save cookies: {e}")


def load_cookies_from_firebase(user_id, driver):
    try:
        doc_ref = db.collection("uber_cookies").document(user_id)
        doc = doc_ref.get()
        if not doc.exists:
            print(f"⚠️ No cookies found for {user_id}")
            return False

        data = doc.to_dict()
        cookies = data.get("cookies", [])
        saved_time_str = data.get("timestamp")

        if saved_time_str:
            saved_time = datetime.fromisoformat(saved_time_str)
            age = datetime.utcnow() - saved_time
            if age > timedelta(hours=24):
                # Delete stale cookies
                doc_ref.delete()
                print(f"🗑️ Cookies deleted for {user_id} (older than 24h)")
                return False

        driver.get("https://m.uber.com/go/home")
        for cookie in cookies:
            try:
                driver.add_cookie(cookie)
            except:
                pass
        driver.refresh()
        print(f"✅ Cookies loaded for {user_id} (last updated {saved_time_str})")
        return True
    except Exception as e:
        print(f"⚠️ Failed to load cookies: {e}")
        return False

# In-memory state. For production, replace with per-user/session storage.
nova_state = {
    "awake": False,
    "waiting_for": "wake",  # wake | command | pickup | dropoff | confirm_booking | login
    "pickup": None,
    "dropoff": None,
    "driver": None,
    "user_id": "test_user",
    "language": "en",
    "listen_language": "en-IN",
    "login_started": False,
}


@app.get("/api/status")
def status():
    return {
        "awake": nova_state["awake"],
        "waiting_for": nova_state["waiting_for"],
        "pickup": nova_state["pickup"],
        "dropoff": nova_state["dropoff"],
        "language": nova_state["language"]
    }


@app.post("/api/start")
def start():
    nova_state["awake"] = False
    nova_state["waiting_for"] = "language_selection"
    nova_state["pickup"] = None
    nova_state["dropoff"] = None
    nova_state["driver"] = None
    nova_state["language"] = "en"
    nova_state["listen_language"] = "en-IN"
    nova_state["login_started"] = False
    return {"response": "Hello! Please choose your preferred language: English or Hindi? / नमस्ते! कृपया अपनी पसंदीदा भाषा चुनें: अंग्रेजी या हिंदी?"}


@app.post("/api/reset")
def reset():
    """Reset the conversation state"""
    if nova_state["driver"]:
        try:
            nova_state["driver"].quit()
        except:
            pass
    nova_state["awake"] = False
    nova_state["waiting_for"] = "language_selection"
    nova_state["pickup"] = None
    nova_state["dropoff"] = None
    nova_state["driver"] = None
    nova_state["language"] = "en"
    nova_state["listen_language"] = "en-IN"
    nova_state["login_started"] = False
    return {"response": "Conversation reset. Please choose your preferred language: English or Hindi?"}


def _setup_driver():
    """Setup Chrome driver with mobile user agent. Headless on servers."""
    options = uc.ChromeOptions()
    options.add_argument(
        "user-agent=Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
        "Mobile/15E148 Safari/604.1"
    )
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev_shm_usage")
    # Headless mode on Render or when HEADLESS=true
    if os.getenv("HEADLESS", "false").lower() == "true" or os.getenv("RENDER"):
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=420,900")
    driver = uc.Chrome(version_main=138, options=options)
    driver.set_window_size(420, 900)
    return driver


def _is_driver_alive(driver):
    try:
        _ = driver.current_url
        return True
    except Exception:
        return False


def _ensure_driver():
    """Return an existing alive driver or create one. Never opens more than one."""
    driver = nova_state.get("driver")
    if driver and _is_driver_alive(driver):
        return driver
    try:
        driver = _setup_driver()
        nova_state["driver"] = driver
        return driver
    except Exception as e:
        print(f"⚠️ Failed to setup driver: {e}")
        return None


def _handle_login_flow():
    """Handle the complete login flow with cookie persistence"""
    try:
        driver = _ensure_driver()
        if not driver:
            return "Failed to setup browser. Please try again."
        
        # Mark that we've started the login so subsequent prompts don't spawn another driver
        nova_state["login_started"] = True
        user_id = nova_state["user_id"]
        
        # Try to load cookies from Firebase
        cookies_loaded = load_cookies_from_firebase(user_id, driver)
        
        if not cookies_loaded:
            # First time login
            driver.get("https://m.uber.com/go/home")
            
            # Check if already logged in
            if not is_logged_in(driver):
                # Click login button and wait for manual login
                click_login_button(driver, lambda text: print(f"🔊 {text}"), selected_language=nova_state["language"])
                
                # Wait for manual login (up to 60 seconds)
                for i in range(30):  # 30 * 2 seconds = 60 seconds
                    if is_logged_in(driver):
                        save_cookies_to_firebase(user_id, driver)
                        return "Login successful! Now let's book your ride. What is your pickup location?"
                    time.sleep(2)
                
                # If login timeout, keep the driver open and ask user to log in manually
                return "Please log in to Uber manually in the browser window that opened. Once logged in, say 'I'm logged in' or 'ready' to continue."
            else:
                save_cookies_to_firebase(user_id, driver)
                return "Already logged in! Let's book your ride. What is your pickup location?"
        else:
            return "Logged in with saved credentials! Let's book your ride. What is your pickup location?"
            
    except Exception as e:
        print(f"⚠️ Login error: {e}")
        return "Login failed. Please try again."


def _handle_location_input(location_text, is_pickup=True):
    """Handle location input from frontend"""
    try:
        driver = nova_state["driver"]
        if not driver:
            return "Browser not ready. Please try again."
        
        wait = WebDriverWait(driver, 20)
        
        if is_pickup:
            # Click pickup button
            pickup_button = wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, '[data-testid="pudo-button-pickup"]'))
            )
            driver.execute_script("arguments[0].click();", pickup_button)
            
            # Enter pickup location
            input_box = wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'input[placeholder="Pickup location"]'))
            )
            input_box.send_keys(location_text)
            time.sleep(2)
            
            # Select first suggestion
            first_option = wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, '[role="option"]'))
            )
            driver.execute_script("arguments[0].click();", first_option)
            
            return "Pickup location set. Where are you going?"
        else:
            # Enter destination
            destination_box = wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'input[placeholder="Dropoff location"]'))
            )
            destination_box.send_keys(location_text)
            time.sleep(2)
            
            # Select first destination suggestion
            dest_suggestion = wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, '[role="option"]'))
            )
            driver.execute_script("arguments[0].click();", dest_suggestion)
            
            return "Destination set. Let me show you the ride options."
            
    except Exception as e:
        print(f"⚠️ Location error: {e}")
        return "Failed to set location. Please try again."


def _handle_ride_options():
    """Handle ride options and selection"""
    try:
        driver = nova_state["driver"]
        if not driver:
            return "Browser not ready. Please try again."
        
        # Wait for ride options to load
        wait = WebDriverWait(driver, 15)
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "li[data-testid='product_selector.list_item']")))
        
        # Get ride options
        ride_blocks = driver.find_elements(By.CSS_SELECTOR, "li[data-testid='product_selector.list_item']")
        
        if not ride_blocks:
            return "No ride options available. Please try again."
        
        # Format ride options for speech
        ride_options = []
        for ride in ride_blocks:
            text = ride.text.strip()
            if text:
                lines = text.split("\n")
                ride_name = lines[0].strip() if lines else text
                price = next((line for line in lines if "₹" in line), "")
                ride_options.append(f"{ride_name} for {price}")
        
        # Return formatted options
        options_text = ". ".join([f"Option {i+1}: {option}" for i, option in enumerate(ride_options)])
        return f"Available rides: {options_text}. Which ride would you like to choose? Say the ride name or option number."
        
    except Exception as e:
        print(f"⚠️ Ride options error: {e}")
        return "Failed to load ride options. Please try again."


def _handle_ride_selection(ride_choice):
    """Handle ride selection and confirmation"""
    try:
        driver = nova_state["driver"]
        if not driver:
            return "Browser not ready. Please try again."
        
        wait = WebDriverWait(driver, 15)
        ride_blocks = driver.find_elements(By.CSS_SELECTOR, "li[data-testid='product_selector.list_item']")
        
        # Find matching ride
        matched_index = None
        for idx, ride in enumerate(ride_blocks):
            text = ride.text.strip()
            if text and (ride_choice.lower() in text.lower() or str(idx + 1) in ride_choice):
                matched_index = idx
                break
        
        if matched_index is None:
            return "Ride not found. Please try again with a different choice."
        
        # Click the selected ride
        ride_element = ride_blocks[matched_index]
        driver.execute_script("arguments[0].scrollIntoView(true);", ride_element)
        time.sleep(0.5)
        driver.execute_script("arguments[0].click();", ride_element)
        
        return "Ride selected! Should I confirm and request this ride? Say yes or no."
        
    except Exception as e:
        print(f"⚠️ Ride selection error: {e}")
        return "Failed to select ride. Please try again."


def _handle_ride_confirmation(confirmation):
    """Handle final ride confirmation and booking"""
    try:
        driver = nova_state["driver"]
        if not driver:
            return "Browser not ready. Please try again."
        
        if any(word in confirmation.lower() for word in ["yes", "confirm", "yeah", "proceed", "haan", "haan ji"]):
            # Click request button
            request_buttons = driver.find_elements(By.XPATH, '//*[@id="wrapper"]/div[1]/div[3]/main/div/section/div[3]/div/div/button')
            for btn in request_buttons:
                if btn.is_displayed() and btn.is_enabled():
                    driver.execute_script("arguments[0].click();", btn)
                    break
            
            time.sleep(2)
            
            # Handle final confirm/cancel popup
            confirm_xpath = '//*[@id="wrapper"]/div[2]/div/div[2]/div/div/div/div/div/div/div[3]/div[2]/button'
            cancel_xpath = '//*[@id="wrapper"]/div[2]/div/div[2]/div/div/div/div/div/div/div[3]/div[1]/button'
            
            try:
                confirm_button = driver.find_element(By.XPATH, confirm_xpath)
                driver.execute_script("arguments[0].click();", confirm_button)
                # Refresh cookies after booking flow
                try:
                    save_cookies_to_firebase(nova_state["user_id"], driver)
                except Exception:
                    pass
                return "Your ride is confirmed! What else can I help you with?"
            except:
                try:
                    save_cookies_to_firebase(nova_state["user_id"], driver)
                except Exception:
                    pass
                return "Ride request sent! What else can I help you with?"
        else:
            return "Booking cancelled. What would you like to do next?"
            
    except Exception as e:
        print(f"⚠️ Ride confirmation error: {e}")
        return "Failed to confirm ride. Please try again."


@app.post("/api/receive-text")
def receive_text(body: TextIn):
    text = (body.text or "").lower().strip()
    print(f"✅ Received from frontend: {text}")

    # Language selection
    if nova_state["waiting_for"] == "language_selection":
        if "english" in text:
            nova_state["language"] = "en"
            nova_state["listen_language"] = "en-IN"
            nova_state["waiting_for"] = "wake"
            response = "Language set to English. Nova is standing by. Say 'wake up Nova' to begin."
            print(f"↩️ Responding: {response}")
            return {"response": response}
        elif "hindi" in text or "हिंदी" in text:
            nova_state["language"] = "hi"
            nova_state["listen_language"] = "hi-IN"
            nova_state["waiting_for"] = "wake"
            response = "भाषा हिंदी में सेट की गई है। नोवा तैयार है। शुरू करने के लिए 'वेक अप नोवा' कहें।"
            print(f"↩️ Responding: {response}")
            return {"response": response}
        else:
            response = "Please say 'English' or 'Hindi' / कृपया 'अंग्रेजी' या 'हिंदी' कहें।"
            print(f"↩️ Responding: {response}")
            return {"response": response}

    # Waiting for wake word
    if nova_state["waiting_for"] == "wake":
        wake_keywords = ["wake up nova", "wake nova", "wake", "wakeup", "वेक अप नोवा", "वेक नोवा"]
        if any(k in text for k in wake_keywords):
            nova_state["awake"] = True
            nova_state["waiting_for"] = "command"
            response = "Nova is now awake, how can I help you?"
            print(f"↩️ Responding: {response}")
            return {"response": response}
        response = "Nova is on standby. Say 'wake up Nova' to begin."
        print(f"↩️ Responding: {response}")
        return {"response": response}

    # After awake, expect a command
    if nova_state["waiting_for"] == "command":
        if ("book" in text and "cab" in text) or ("book" in text and "ride" in text) or ("open" in text and "uber" in text) or ("कैब" in text and "बुक" in text):
            nova_state["waiting_for"] = "login"
            response = "Opening Uber. Please wait while I set up the browser and check your login status."
            print(f"↩️ Responding: {response}")
            return {"response": response}
        elif "change language" in text or "भाषा बदलो" in text:
            nova_state["waiting_for"] = "language_selection"
            response = "Please choose your preferred language: English or Hindi? / कृपया अपनी पसंदीदा भाषा चुनें: अंग्रेजी या हिंदी?"
            print(f"↩️ Responding: {response}")
            return {"response": response}
        response = "I didn't understand that. You can say 'book a cab' to start booking."
        print(f"↩️ Responding: {response}")
        return {"response": response}

    # Handle login flow
    if nova_state["waiting_for"] == "login":
        # If we've already opened a driver for login, don't reopen; just check status
        existing_driver = nova_state.get("driver")
        if nova_state.get("login_started") and existing_driver:
            try:
                if is_logged_in(existing_driver):
                    save_cookies_to_firebase(nova_state["user_id"], existing_driver)
                    nova_state["waiting_for"] = "pickup"
                    response = "You're already logged in. What is your pickup location?"
                    print(f"↩️ Responding: {response}")
                    return {"response": response}
            except Exception:
                pass
        response = _handle_login_flow()
        response_lower = (response or "").lower()
        if "successful" in response_lower or "logged in" in response_lower:
            nova_state["waiting_for"] = "pickup"
        elif "manually" in response_lower:
            nova_state["waiting_for"] = "manual_login_wait"
        print(f"↩️ Responding: {response}")
        return {"response": response}

    # Handle manual login confirmation
    if nova_state["waiting_for"] == "manual_login_wait":
        if any(word in text for word in ["logged in", "ready", "done", "finished", "complete", "हो गया", "तैयार"]):
            driver = nova_state["driver"]
            if driver and is_logged_in(driver):
                save_cookies_to_firebase(nova_state["user_id"], driver)
                nova_state["waiting_for"] = "pickup"
                response = "Great! You're logged in. Now let's book your ride. What is your pickup location?"
            else:
                response = "I don't see that you're logged in yet. Please complete the login process in the browser window and then say 'ready'."
        else:
            response = "Please complete the login process in the browser window and then say 'ready' or 'I'm logged in'."
        print(f"↩️ Responding: {response}")
        return {"response": response}

    # Collect pickup
    if nova_state["waiting_for"] == "pickup":
        # Ensure the driver is available (reuse existing, don't open extra if alive)
        if not nova_state.get("driver") or not _is_driver_alive(nova_state.get("driver")):
            # Attempt to prepare a driver silently if none; this will only create one if missing
            _ = _ensure_driver()
        # Ignore our own known prompts that might be transcribed by mistake
        if any(phrase in text for phrase in [
            "what is your pickup location",
            "what is your pick up location",
            "please say your pickup location",
        ]):
            response = "I'm listening. Please tell me your pickup location."
            print(f"↩️ Responding: {response}")
            return {"response": response}
        if len(text) >= 3:
            nova_state["pickup"] = text
            response = _handle_location_input(text, is_pickup=True)
            if "Pickup location set" in response:
                nova_state["waiting_for"] = "dropoff"
            print(f"↩️ Responding: {response}")
            return {"response": response}
        response = "Please say your pickup location again."
        print(f"↩️ Responding: {response}")
        return {"response": response}

    # Collect dropoff
    if nova_state["waiting_for"] == "dropoff":
        # Ignore our own known prompts that might be transcribed by mistake
        if any(phrase in text for phrase in [
            "where are you going",
            "please say your drop location",
            "please say your dropoff location",
        ]):
            response = "I'm listening. Please tell me your drop location."
            print(f"↩️ Responding: {response}")
            return {"response": response}
        if len(text) >= 3:
            nova_state["dropoff"] = text
            response = _handle_location_input(text, is_pickup=False)
            if "Destination set" in response:
                nova_state["waiting_for"] = "ride_options"
            print(f"↩️ Responding: {response}")
            return {"response": response}
        response = "Please say your drop location again."
        print(f"↩️ Responding: {response}")
        return {"response": response}

    # Handle ride options
    if nova_state["waiting_for"] == "ride_options":
        response = _handle_ride_options()
        nova_state["waiting_for"] = "ride_selection"
        print(f"↩️ Responding: {response}")
        return {"response": response}

    # Handle ride selection
    if nova_state["waiting_for"] == "ride_selection":
        response = _handle_ride_selection(text)
        if "Ride selected" in response:
            nova_state["waiting_for"] = "confirm_booking"
        print(f"↩️ Responding: {response}")
        return {"response": response}

    # Confirm booking
    if nova_state["waiting_for"] == "confirm_booking":
        response = _handle_ride_confirmation(text)
        if "confirmed" in response or "sent" in response or "cancelled" in response:
            nova_state["waiting_for"] = "command"
            # Clean up driver
            if nova_state["driver"]:
                try:
                    nova_state["driver"].quit()
                except:
                    pass
                nova_state["driver"] = None
        print(f"↩️ Responding: {response}")
        return {"response": response}   

    # Fallback
    response = "I didn't catch that. Please try again."
    print(f"↩️ Responding: {response}")
    return {"response": response}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)


