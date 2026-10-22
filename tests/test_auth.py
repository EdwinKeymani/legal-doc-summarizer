"""Registration, sign-in, sign-out and password reset."""
import re
import uuid

from playwright.sync_api import Page, expect

from conftest import PASSWORD, register, sign_in, submit_form_with


def fresh_email():
    return f"user.{uuid.uuid4().hex[:8]}@example.com"


def test_health_endpoint(page: Page):
    response = page.goto("/health")
    assert response.status == 200
    expect(page.locator("body")).to_have_text("OK")


def test_anonymous_visitor_is_sent_to_login(page: Page):
    page.goto("/dashboard")
    expect(page).to_have_url(re.compile(r"/login"))


def test_register_signs_you_in_and_logout_signs_you_out(page: Page):
    email = fresh_email()
    register(page, "Test Person", email)
    expect(page).to_have_url(re.compile(r"/dashboard"))

    page.goto("/logout")
    expect(page).to_have_url(re.compile(r"/login"))
    page.goto("/dashboard")
    expect(page).to_have_url(re.compile(r"/login"))      # really signed out


def test_register_rejects_a_weak_password(page: Page):
    register(page, "Test Person", fresh_email(), password="short1")
    expect(page.get_by_text("Password must be at least 8 characters.")).to_be_visible()
    expect(page).not_to_have_url(re.compile(r"/dashboard"))


def test_register_requires_letter_and_number(page: Page):
    register(page, "Test Person", fresh_email(), password="onlyletters")
    expect(page.get_by_text("at least one letter and one number")).to_be_visible()


def test_register_rejects_a_duplicate_email(page: Page, users):
    register(page, "Someone Else", users["member"]["email"])
    expect(page.get_by_text("An account with that email already exists")).to_be_visible()


def test_login_with_wrong_password_is_refused(page: Page, users):
    sign_in(page, users["member"]["email"], password="Wrongpass999")
    expect(page.get_by_text("Invalid email or password.")).to_be_visible()
    expect(page).not_to_have_url(re.compile(r"/dashboard"))


def test_unknown_email_gets_the_same_message(page: Page):
    """The app must not reveal whether an account exists."""
    sign_in(page, fresh_email(), password="Whatever123")
    expect(page.get_by_text("Invalid email or password.")).to_be_visible()


def test_login_with_correct_password(page: Page, users):
    sign_in(page, users["member"]["email"])
    expect(page).to_have_url(re.compile(r"/dashboard"))


def test_password_reset_end_to_end(page: Page):
    """Forgot password -> reset link (shown on screen because SHOW_RESET_LINK=1)
    -> new password works, old one does not, the link cannot be reused."""
    email = fresh_email()
    register(page, "Reset Person", email)
    page.goto("/logout")

    page.goto("/forgot-password")
    page.fill("[name=email]", email)
    submit_form_with(page, "email")
    expect(page.get_by_text("a reset link has been generated")).to_be_visible()

    page_text = page.locator("body").inner_text()
    link = re.search(r"https?://\S+/reset-password/[\w-]+", page_text)
    assert link, "The dev-mode reset link was not shown. Is SHOW_RESET_LINK=1 set for the test server?"
    reset_path = "/reset-password/" + link.group(0).rsplit("/", 1)[1]

    new_password = "Brandnew456"
    page.goto(reset_path)
    page.fill("[name=new_password]", new_password)
    page.fill("[name=confirm_password]", new_password)
    submit_form_with(page, "new_password")
    expect(page.get_by_text("Password reset successfully")).to_be_visible()

    sign_in(page, email, password=PASSWORD)               # old password
    expect(page.get_by_text("Invalid email or password.")).to_be_visible()
    sign_in(page, email, password=new_password)           # new password
    expect(page).to_have_url(re.compile(r"/dashboard"))

    page.goto("/logout")
    page.goto(reset_path)                                  # token already used
    expect(page.get_by_text("invalid or has expired")).to_be_visible()


def test_reset_for_unknown_email_looks_identical(page: Page):
    page.goto("/forgot-password")
    page.fill("[name=email]", fresh_email())
    submit_form_with(page, "email")
    expect(page.get_by_text("a reset link has been generated")).to_be_visible()
    assert "/reset-password/" not in page.locator("body").inner_text()


def test_reset_with_mismatched_passwords_is_refused(page: Page):
    email = fresh_email()
    register(page, "Mismatch Person", email)
    page.goto("/logout")
    page.goto("/forgot-password")
    page.fill("[name=email]", email)
    submit_form_with(page, "email")
    link = re.search(r"/reset-password/[\w-]+", page.locator("body").inner_text())
    page.goto(link.group(0))
    page.fill("[name=new_password]", "Brandnew456")
    page.fill("[name=confirm_password]", "Different789")
    submit_form_with(page, "new_password")
    expect(page.get_by_text("Passwords do not match.")).to_be_visible()
