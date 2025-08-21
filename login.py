from selenium.webdriver.common.by import By
from selenium.common.exceptions import NoSuchElementException
import time

# === Check if user is already logged in ===
def is_logged_in(driver):
    try:
        # Mobile: if "Login" button is present, user is NOT logged in
        driver.find_element(By.CSS_SELECTOR, "button.css-dHHA-DQ")
        return False
    except NoSuchElementException:
        return True

# === Click login button if needed ===
def click_login_button(driver, speak_func, selected_language="en"):
    if is_logged_in(driver):
        if selected_language == "hi":
            speak_func("आप पहले से लॉग इन हैं। लॉगिन छोड़ रहा हूँ।", lang=selected_language)
        else:
            speak_func("You're already logged in. Skipping login.", lang=selected_language)
        return

    if selected_language == "hi":
        speak_func("ऐसा लगता है कि आपने अभी तक लॉग इन नहीं किया है। कृपया मैन्युअल रूप से लॉग इन करें।", lang=selected_language)
    else:
        speak_func("It looks like you're not logged in yet. Please log in manually.", lang=selected_language)

    try:
        login_btn = driver.find_element(By.CSS_SELECTOR, "button.css-dHHA-DQ")
        driver.execute_script("arguments[0].click();", login_btn)
    except Exception as e:
        print(f"⚠️ Failed to click login button: {e}")
        if selected_language == "hi":
            speak_func("लॉगिन बटन पर क्लिक नहीं कर सका। कृपया मैन्युअल रूप से प्रयास करें।", lang=selected_language)
        else:
            speak_func("Couldn't click the login button. Please try manually.", lang=selected_language)

    # Wait for manual login
    for i in range(60):
        if is_logged_in(driver):
            if selected_language == "hi":
                speak_func("लॉगिन का पता चला। आप अब लॉग इन हैं।", lang=selected_language)
            else:
                speak_func("Login detected. You're now logged in.", lang=selected_language)
            return
        time.sleep(2)

    if selected_language == "hi":
        speak_func("समय के भीतर लॉगिन का पता नहीं चला। कृपया पुनः प्रयास करें।", lang=selected_language)
    else:
        speak_func("Login not detected within time. Please try again.", lang=selected_language)
