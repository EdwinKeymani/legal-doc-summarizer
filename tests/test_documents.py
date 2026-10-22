"""Upload, view, export, download and delete, plus who may touch which document."""
import re

from playwright.sync_api import Page, expect

from conftest import RUN_ID, SAMPLE_CONTRACT, csrf_token, submit_form_with, upload_contract


def test_upload_processes_a_contract(member_page: Page):
    document_id = upload_contract(member_page)
    expect(member_page).to_have_url(re.compile(rf"/document/{document_id}$"))
    # No Gemini key in tests, so the summary is produced but clauses stay "Review"
    expect(member_page.get_by_text(re.compile("processed successfully|risk assessment is temporarily unavailable"))).to_be_visible()
    body = member_page.locator("body").inner_text()
    assert "could not be processed" not in body
    # Clauses the local rules should find in the sample contract
    for category in ("Termination", "Indemnification", "Confidentiality"):
        assert category in body, f"Expected a '{category}' clause on the report page"


def test_upload_rejects_an_unsupported_file_type(member_page: Page):
    member_page.goto("/dashboard")
    member_page.set_input_files("input[name=document]", files=[
        {"name": "malware.exe", "mimeType": "application/octet-stream", "buffer": b"MZ not a contract"}])
    submit_form_with(member_page, "document")
    expect(member_page.get_by_text("Unsupported file type")).to_be_visible()


def test_upload_with_no_file_is_refused(member_page: Page):
    member_page.goto("/dashboard")
    submit_form_with(member_page, "document")
    expect(member_page.get_by_text("No file was selected.")).to_be_visible()


def test_empty_text_file_fails_with_a_reason(member_page: Page):
    member_page.goto("/dashboard")
    member_page.set_input_files("input[name=document]", files=[
        {"name": f"pw_{RUN_ID}_empty.txt", "mimeType": "text/plain", "buffer": b"   \n"}])
    submit_form_with(member_page, "document")
    member_page.wait_for_url(re.compile(r"/document/\d+"))
    expect(member_page.get_by_text("No extractable text was found")).to_be_visible()


def test_document_appears_on_the_dashboard(member_page: Page, member_document):
    member_page.goto("/dashboard")
    expect(member_page.get_by_text(f"pw_{RUN_ID}_sample_contract.txt").first).to_be_visible()


def test_export_pdf(member_page: Page, member_document):
    response = member_page.request.get(f"/document/{member_document}/export/pdf")
    assert response.status == 200
    assert response.headers["content-type"].startswith("application/pdf")
    assert "attachment" in response.headers["content-disposition"]
    assert response.body()[:5] == b"%PDF-"


def test_export_word(member_page: Page, member_document):
    response = member_page.request.get(f"/document/{member_document}/export/docx")
    assert response.status == 200
    assert "wordprocessingml" in response.headers["content-type"]
    assert "attachment" in response.headers["content-disposition"]
    assert response.body()[:2] == b"PK"        # a .docx is a zip file


def test_export_unknown_format_is_404(member_page: Page, member_document):
    assert member_page.request.get(f"/document/{member_document}/export/exe").status == 404


def test_download_original_file(member_page: Page, member_document):
    response = member_page.request.get(f"/document/{member_document}/download")
    assert response.status == 200
    assert response.body() == SAMPLE_CONTRACT.read_bytes()


def test_anonymous_cannot_export(page: Page, member_document):
    response = page.request.get(f"/document/{member_document}/export/pdf", max_redirects=0)
    assert response.status == 302
    assert "/login" in response.headers["location"]


def test_other_member_cannot_see_export_or_download(other_page: Page, member_document):
    """Someone else's document looks exactly like a missing one (404)."""
    assert other_page.goto(f"/document/{member_document}").status == 404
    assert other_page.request.get(f"/document/{member_document}/export/pdf").status == 404
    assert other_page.request.get(f"/document/{member_document}/export/docx").status == 404
    assert other_page.request.get(f"/document/{member_document}/download").status == 404


def test_other_member_cannot_delete(other_page: Page, member_document):
    other_page.goto("/dashboard")
    response = other_page.request.post(
        f"/document/{member_document}/delete",
        headers={"X-CSRFToken": csrf_token(other_page)}, max_redirects=0)
    assert response.status == 404


def test_owner_can_delete_a_document(member_page: Page):
    document_id = upload_contract(member_page, file_name=f"pw_{RUN_ID}_to_delete.txt")
    member_page.on("dialog", lambda dialog: dialog.accept())     # "Are you sure?" prompts
    delete_form = member_page.locator(f'form[action$="/document/{document_id}/delete"]')
    delete_form.locator("button, input[type=submit]").first.click()
    expect(member_page.get_by_text("was deleted.")).to_be_visible()
    assert member_page.goto(f"/document/{document_id}").status == 404
