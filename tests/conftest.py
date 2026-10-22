"""
Test harness for Chambua.

What it does
  * Starts the real app (flask run) on its own port, against a THROWAWAY
    SQLite database in a temp folder. Your real legal.db is never touched.
  * Blanks GEMINI_API_KEY for the run, so no network calls, no quota use and
    no flaky AI output: clauses are always marked "Review".
  * Turns SHOW_RESET_LINK on so the password-reset test can read the link
    from the page.
  * Registers three users once (two Members and one Administrator) and hands
    their credentials to the tests.

If a page changes its HTML, the places to fix are the small helpers in this
file (register, sign_in, submit_form_with), not every test.
"""
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
SAMPLE_CONTRACT = PROJECT_DIR / "tests" / "fixtures" / "sample_contract.txt"
PORT = int(os.environ.get("TEST_PORT", "5055"))
PASSWORD = "Testpass123"
RUN_ID = uuid.uuid4().hex[:8]          # makes emails and file names unique per run


# ---------------------------------------------------------------------------
# Helpers (the only code that knows what the pages look like)
# ---------------------------------------------------------------------------
def submit_form(form):
    """Submit a form the way a user would: click its button if it is visible,
    otherwise (button inside a closed panel, hidden input-only form, ...)
    submit the form directly. Either way the page's own submit handlers run."""
    form.evaluate("f => { f.noValidate = true; }")   # test the SERVER-side rules, not the browser's
    button = form.locator("button[type=submit], input[type=submit], button:not([type])").first
    if button.count() and button.is_visible():
        button.click()
    else:
        form.evaluate("f => f.requestSubmit()")


def submit_form_with(page, field_name):
    """Submit the form that contains the field [name=field_name]."""
    submit_form(page.locator("form").filter(has=page.locator(f"[name={field_name}]")).first)


def csrf_token(page):
    """The CSRF token of the current session, read from the page the browser is on."""
    meta = page.locator("meta[name=csrf-token]")
    if meta.count():
        return meta.first.get_attribute("content")
    return page.locator("input[name=csrf_token]").first.get_attribute("value")


def register(page, full_name, email, password=PASSWORD):
    page.goto("/register")
    page.fill("[name=full_name]", full_name)
    page.fill("[name=email]", email)
    page.fill("[name=password]", password)
    submit_form_with(page, "password")


def sign_in(page, email, password=PASSWORD):
    page.goto("/login")
    page.fill("[name=email]", email)
    page.fill("[name=password]", password)
    submit_form_with(page, "password")


def upload_contract(page, path=SAMPLE_CONTRACT, file_name=None, mode="Extractive - Fast"):
    """Upload through the dashboard form. Returns the new document's id.
    file_name lets a test upload the same content under a different name."""
    page.goto("/dashboard")
    if file_name is None:
        file_name = f"pw_{RUN_ID}_{Path(path).name}"
    page.set_input_files("input[name=document]", files=[{
        "name": file_name,
        "mimeType": "text/plain",
        "buffer": Path(path).read_bytes(),
    }])
    select = page.locator("select[name=extraction_mode]")
    if select.count():
        select.first.select_option(mode, force=True)   # the panel may be collapsed
    submit_form_with(page, "document")
    page.wait_for_url(re.compile(r"/document/\d+"))
    return int(re.search(r"/document/(\d+)", page.url).group(1))


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------
def _server_env(database_file):
    env = os.environ.copy()
    for name in ("RENDER", "VERCEL", "INITIAL_ADMIN_EMAIL", "DATABASE_URL"):
        env.pop(name, None)
    env.update({
        "SECRET_KEY": "test-secret-not-for-production",
        "DATABASE_URL": "sqlite:///" + Path(database_file).as_posix(),
        "GEMINI_API_KEY": "",            # python-dotenv will not override an existing (even empty) value
        "SHOW_RESET_LINK": "1",
        "FLASK_DEBUG": "0",
        "FORCE_SECURE_COOKIES": "0",
        "PYTHONUNBUFFERED": "1",
    })
    return env


@pytest.fixture(scope="session")
def server():
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", PORT)) == 0:
            pytest.fail(f"Port {PORT} is already in use, probably by a server left over from an earlier run. "
                        "Close it (or set TEST_PORT=5056) and run again.")
    with tempfile.TemporaryDirectory(prefix="chambua_test_") as temp_dir:
        env = _server_env(Path(temp_dir) / "test.db")
        log_path = Path(temp_dir) / "server.log"      # a file, not a pipe: a full pipe would stall the app
        log_file = open(log_path, "w", encoding="utf-8")
        process = subprocess.Popen(
            [sys.executable, "-m", "flask", "--app", "app", "run", "--port", str(PORT), "--no-reload"],
            cwd=PROJECT_DIR, env=env, stdout=log_file, stderr=subprocess.STDOUT, text=True,
        )
        url = f"http://127.0.0.1:{PORT}"
        deadline = time.time() + 90              # first start applies the migrations
        while time.time() < deadline:
            if process.poll() is not None:
                pytest.fail("The app exited during start-up:\n" + log_path.read_text(encoding="utf-8", errors="replace"))
            try:
                if urllib.request.urlopen(url + "/health", timeout=2).status == 200:
                    break
            except OSError:
                time.sleep(0.5)
        else:
            process.terminate()
            pytest.fail("The app did not answer /health within 90 seconds:\n" + log_path.read_text(encoding="utf-8", errors="replace"))

        yield {"url": url, "env": env}

        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        log_file.close()
        # Uploaded test files go to the project's uploads/ folder; remove ours.
        for leftover in (PROJECT_DIR / "uploads").glob(f"*pw_{RUN_ID}*"):
            leftover.unlink(missing_ok=True)


@pytest.fixture(scope="session")
def base_url(server):
    """pytest-playwright uses this, so page.goto('/login') just works."""
    return server["url"]


# ---------------------------------------------------------------------------
# Users and a ready-made document
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def users(server, browser, base_url):
    people = {
        "member": {"name": "Amina Member", "email": f"amina.{RUN_ID}@example.com"},
        "other": {"name": "Otieno Other", "email": f"otieno.{RUN_ID}@example.com"},
        "admin": {"name": "Wanjiru Admin", "email": f"wanjiru.{RUN_ID}@example.com"},
    }
    for person in people.values():
        person["password"] = PASSWORD
        context = browser.new_context(base_url=base_url)
        page = context.new_page()
        register(page, person["name"], person["email"])
        page.wait_for_url(re.compile(r"/dashboard"))
        context.close()

    # Registration always creates a Member, so promote the admin the same way
    # the README does. The role is re-read on every request: no restart needed.
    subprocess.run(
        [sys.executable, "-m", "flask", "--app", "app", "create-admin", people["admin"]["email"]],
        cwd=PROJECT_DIR, env=server["env"], check=True, capture_output=True, text=True,
    )
    return people


@pytest.fixture(scope="session")
def member_document(users, browser, base_url):
    """One processed document owned by users['member'], shared by read-only tests."""
    context = browser.new_context(base_url=base_url)
    page = context.new_page()
    sign_in(page, users["member"]["email"])
    page.wait_for_url(re.compile(r"/dashboard"))
    document_id = upload_contract(page)
    context.close()
    return document_id


@pytest.fixture
def member_page(page, users):
    sign_in(page, users["member"]["email"])
    page.wait_for_url(re.compile(r"/dashboard"))
    return page


@pytest.fixture
def other_page(page, users):
    sign_in(page, users["other"]["email"])
    page.wait_for_url(re.compile(r"/dashboard"))
    return page


@pytest.fixture
def admin_page(page, users):
    sign_in(page, users["admin"]["email"])
    page.wait_for_url(re.compile(r"/dashboard"))
    return page


@pytest.fixture(autouse=True)
def quicker_timeouts(page):
    """A wrong selector should fail in 8 seconds, not 30."""
    page.set_default_timeout(8000)
