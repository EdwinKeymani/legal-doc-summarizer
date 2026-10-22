"""Who may open which page. A refused page must look exactly like a missing one (404)."""
import re
import uuid

from playwright.sync_api import Page, expect

from conftest import csrf_token, sign_in, submit_form_with

ADMIN_PAGES = ["/admin/users", "/admin/roles", "/admin/audit"]


def test_member_gets_404_on_every_admin_page(member_page: Page):
    for path in ADMIN_PAGES:
        assert member_page.goto(path).status == 404, f"{path} should be hidden from Members"


def test_anonymous_visitor_is_sent_to_login_not_404(page: Page):
    for path in ADMIN_PAGES:
        page.goto(path)
        expect(page).to_have_url(re.compile(r"/login"))


def test_admin_can_open_every_admin_page(admin_page: Page):
    for path in ADMIN_PAGES:
        assert admin_page.goto(path).status == 200, f"{path} should open for an Administrator"


def test_admin_dashboard_is_not_the_member_dashboard(admin_page: Page):
    """Administrators manage people; they do not upload contracts."""
    admin_page.goto("/dashboard")
    assert admin_page.locator("input[name=document]").count() == 0


def test_admin_cannot_read_a_members_document(admin_page: Page, member_document):
    """Administrators never read contract text, even by guessing the URL."""
    assert admin_page.goto(f"/document/{member_document}").status == 404
    assert admin_page.request.get(f"/document/{member_document}/export/pdf").status == 404
    assert admin_page.request.get(f"/document/{member_document}/download").status == 404


def test_admin_cannot_upload(admin_page: Page):
    admin_page.goto("/admin/users")                      # any page with a form carries a valid token
    response = admin_page.request.post(
        "/upload", headers={"X-CSRFToken": csrf_token(admin_page)}, max_redirects=0,
        multipart={"document": {"name": "x.txt", "mimeType": "text/plain", "buffer": b"hello"}})
    assert response.status == 404


def test_registration_form_cannot_choose_a_role(page: Page):
    """Even if someone adds a role field to the form, they still get a Member."""
    email = f"sneaky.{uuid.uuid4().hex[:8]}@example.com"
    page.goto("/register")
    page.fill("[name=full_name]", "Sneaky Person")
    page.fill("[name=email]", email)
    page.fill("[name=password]", "Testpass123")
    page.evaluate("""() => {
        const form = document.querySelector('input[name=password]').form;
        const field = document.createElement('input');
        field.type = 'hidden'; field.name = 'role'; field.value = 'admin';
        form.appendChild(field);
    }""")
    submit_form_with(page, "password")
    page.wait_for_url(re.compile(r"/dashboard"))
    assert page.goto("/admin/users").status == 404


def test_refused_pages_are_audited(users, member_page: Page, browser, base_url):
    """A refused request is recorded, and an Administrator can find it."""
    member_page.goto("/admin/users")                       # refused (404) and logged
    context = browser.new_context(base_url=base_url)
    admin = context.new_page()
    sign_in(admin, users["admin"]["email"])
    admin.goto("/admin/audit?person=" + users["member"]["email"])
    shown = admin.locator("body").inner_text()
    assert users["member"]["email"] in shown or users["member"]["name"] in shown, \
        "The audit page should list the refused request for this person"
    context.close()
