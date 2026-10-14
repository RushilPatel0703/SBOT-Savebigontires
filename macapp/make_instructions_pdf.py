#!/usr/bin/env python3
"""Write "Install Instructions.pdf", the guide shipped in the client's zip.

It is written for the client's AI assistant as much as for the client: every
step says who has to do it. macOS reserves some steps for the person at the
keyboard (security approvals, their password), and an assistant that tries to
do those itself gets stuck, so they are marked CLIENT and explained, rather
than left for the assistant to discover.

    python macapp/make_instructions_pdf.py --out guide.pdf --version 2026.10.14 --min-macos 14.0
"""

import argparse

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

INK = colors.HexColor("#16202B")
MUTED = colors.HexColor("#5B6775")
BLUE = colors.HexColor("#1C83E1")
CLIENT_BG = colors.HexColor("#FDECEC")
CLIENT_FG = colors.HexColor("#B42318")
AI_BG = colors.HexColor("#E8F2FC")
AI_FG = colors.HexColor("#175CD3")
RULE = colors.HexColor("#D9DEE5")
CODE_BG = colors.HexColor("#F3F6FA")

ss = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=ss["Title"], fontSize=22, leading=26, textColor=INK,
                    alignment=0, spaceAfter=4)
SUB = ParagraphStyle("Sub", parent=ss["Normal"], fontSize=10, textColor=MUTED, spaceAfter=14)
H2 = ParagraphStyle("H2", parent=ss["Heading2"], fontSize=14, leading=18, textColor=INK,
                    spaceBefore=14, spaceAfter=6)
BODY = ParagraphStyle("Body", parent=ss["Normal"], fontSize=10, leading=14, textColor=INK,
                      spaceAfter=6)
SMALL = ParagraphStyle("Small", parent=BODY, fontSize=9, leading=12, textColor=MUTED)
CODE = ParagraphStyle("Code", parent=BODY, fontName="Courier", fontSize=9, leading=12,
                      backColor=CODE_BG, borderPadding=(4, 6, 4, 6), spaceBefore=4, spaceAfter=8)
BADGE = ParagraphStyle("Badge", parent=BODY, fontName="Helvetica-Bold", fontSize=8, leading=10,
                       alignment=TA_CENTER, spaceAfter=0)
STEP_TITLE = ParagraphStyle("StepTitle", parent=BODY, fontName="Helvetica-Bold", spaceAfter=3)

WHO = {
    "CLIENT": ("CLIENT", CLIENT_BG, CLIENT_FG),
    "AI": ("AI CAN GUIDE", AI_BG, AI_FG),
}


def p(text, style=BODY):
    return Paragraph(text, style)


def code(text):
    return Paragraph(text.replace("&", "&amp;").replace("<", "&lt;"), CODE)


def step(number, who, title, *body):
    label, bg, fg = WHO[who]
    badge = Table([[Paragraph(f'<font color="{fg.hexval()}">{label}</font>', BADGE)]],
                  colWidths=[0.95 * inch])
    badge.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    content = [p(f"{number}. {title}", STEP_TITLE)]
    content += [b if not isinstance(b, str) else p(b) for b in body]
    row = Table([[badge, content]], colWidths=[1.1 * inch, 5.4 * inch])
    row.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, RULE),
    ]))
    return KeepTogether([row])


def table(rows, widths):
    t = Table([[p(c, SMALL if i else BODY) for i, c in enumerate(r)] for r in rows],
              colWidths=widths)
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
    ]))
    return t


def build(out, version, min_macos):
    story = [
        p("Save Big On Tires — Mac App", H1),
        p(f"Installation and usage guide &nbsp;·&nbsp; version {version}", SUB),

        p("<b>Who this is for.</b> This guide is written so an AI assistant can walk the "
          "owner of this Mac through installing and using the app. Each step is marked:"),
        table([
            ["<font color='#B42318'><b>CLIENT</b></font>",
             "Only the person at the Mac can do it: a macOS security approval, their "
             "password or Touch ID. macOS deliberately blocks software, including an AI "
             "assistant, from clicking these. The assistant should explain the step and "
             "wait for the person to confirm it is done."],
            ["<font color='#175CD3'><b>AI CAN GUIDE</b></font>",
             "Ordinary steps. The assistant can explain them, or carry them out if it has "
             "control of the Mac."],
        ], [1.2 * inch, 5.3 * inch]),
        Spacer(1, 6),
        p("<b>What the app does.</b> It logs into the five tire and wheel suppliers, "
          "downloads their inventory, cleans it, and builds Shopify product import files "
          "(CSV). It runs in the background and is used through a web page on this Mac "
          "only (http://localhost:8642). Nothing is sent to Shopify automatically: the "
          "import files are uploaded by hand. Every product in those files is tagged with "
          "the supplier it came from (for example <i>Supplier_Dttire</i>)."),

        p("Before you start", H2),
        table([
            ["Mac", "Apple Silicon (M1, M2, M3, M4 or newer). Intel Macs are not supported. "
                    "To check: Apple menu > About This Mac > the <i>Chip</i> line says Apple M..."],
            ["macOS", f"{min_macos} or newer (Apple menu > About This Mac)."],
            ["Disk space", "About 2 GB free."],
            ["Internet", "Needed on first launch and for every supplier run."],
            ["Account", "Installing into Applications may ask for an administrator password."],
        ], [1.2 * inch, 5.3 * inch]),

        p("Install", H2),
        step(1, "AI", "Unzip the download",
             "Double-click <b>SaveBigOnTires-mac.zip</b> (usually in Downloads). A folder "
             "named <b>Save Big On Tires</b> appears, containing <b>Save Big On Tires.app</b> "
             "and this PDF."),
        step(2, "CLIENT", "Move the app into Applications",
             "Drag <b>Save Big On Tires.app</b> onto <b>Applications</b> in the Finder "
             "sidebar. If macOS asks to authenticate, enter the Mac's password. If it asks "
             "whether to replace an older copy, choose <b>Replace</b>; settings and data "
             "are kept (see <i>Updating</i>)."),
        step(3, "CLIENT", "Approve the app the first time it opens",
             "The app is not distributed through the App Store, so macOS blocks it until "
             "the owner approves it once:",
             "a) In Applications, double-click <b>Save Big On Tires</b>. macOS says it "
             "cannot verify the app. Click <b>Done</b> (or <b>OK</b>). Do <b>not</b> click "
             "<i>Move to Trash</i>.",
             "b) Open <b>System Settings > Privacy &amp; Security</b>, scroll down to "
             "<b>Security</b>, and click <b>Open Anyway</b> next to the message about "
             "Save Big On Tires. Confirm with the password or Touch ID, then click "
             "<b>Open Anyway</b> again.",
             "This is needed only once per installed version. On macOS 14, right-click the "
             "app > <b>Open</b> > <b>Open</b> also works.",
             p("<i>For an assistant with Terminal access:</i> with the owner's explicit "
               "agreement, this command does the same as step 3 and avoids the dialogs. "
               "Run it after step 2:", SMALL),
             code('xattr -dr com.apple.quarantine "/Applications/Save Big On Tires.app"')),
        step(4, "AI", "Wait for the first-time setup",
             "The app has no window and no Dock icon; this is normal. On the first launch "
             "it downloads a browser component it uses to log into suppliers (about 150 MB, "
             "1 to 3 minutes). A notification may say so. When it is ready, the dashboard "
             "opens in the default web browser at <b>http://localhost:8642</b>."),
        step(5, "CLIENT", "Allow macOS prompts if any appear",
             "If macOS asks whether Save Big On Tires may send notifications, either answer "
             "is fine. If it asks to access a folder, click <b>Allow</b>. The app only "
             "works inside its own folder (see <i>Where things are</i>). It never needs "
             "Accessibility, Screen Recording, Full Disk Access, or a firewall exception."),

        p("Using the app", H2),
        p("Work through the tabs from left to right:"),
        table([
            ["1 · Cookies", "Click <b>Refresh all cookies</b> before every run. It logs into "
                            "the suppliers in the background and takes about a minute."],
            ["2 · Suppliers", "Check the markup per supplier, then start the supplier "
                              "pipelines. North Country takes about 8 hours; the others are "
                              "much faster. The sidebar shows progress."],
            ["3 · Enrichment", "Optional. Fills in missing product images and specs. Cached "
                               "results are free. <b>New searches cost money</b> on the "
                               "owner's SerpAPI account and ask for a typed confirmation. "
                               "An assistant must never confirm a paid run without the "
                               "owner's explicit approval for that run."],
            ["4 · Upload", "Click <b>Prepare upload files</b>. The CSVs appear in the "
                           "<b>Uploads</b> folder (button: <i>Open latest folder</i>). Import "
                           "them in Shopify Admin > Products > Import. If there are several "
                           "files, import them in the numbered order."],
            ["Advanced", "Clear a pipeline stuck on 'Running', or <b>Close the app</b>."],
        ], [1.2 * inch, 5.3 * inch]),
        Spacer(1, 6),
        step(6, "CLIENT", "Keep the Mac awake during long runs",
             "While the app is open it stops the Mac from idle-sleeping. It cannot stop "
             "sleep caused by <b>closing the lid</b>, logging out, restarting or shutting "
             "down, and any of these stops a run in progress. For a full run, keep the "
             "MacBook <b>plugged in with the lid open</b>."),
        step(7, "AI", "Close and reopen",
             "To stop the app: Advanced tab > <b>Close the app</b>. Supplier runs already in "
             "progress keep going in the background. To open the dashboard again, "
             "double-click the app. If it is already running, the dashboard tab simply "
             "reopens."),

        p("Where things are", H2),
        table([
            ["~/SaveBigOnTires/Uploads", "The Shopify import files, one dated folder per "
                                         "build. Safe to copy, move or delete."],
            ["~/SaveBigOnTires/.engine", "Hidden. The app's working data: supplier "
                                         "downloads, markup settings, caches, logs. Do not "
                                         "edit or delete it. Deleting it resets the app, "
                                         "including the markup settings and the paid "
                                         "enrichment cache."],
            ["Logs", "~/SaveBigOnTires/.engine/app_logs/ (app start-up) and "
                     "~/SaveBigOnTires/.engine/pipelines/dashboard_logs/ (each run). The "
                     "<i>View log</i> buttons in the dashboard show the same logs."],
        ], [1.9 * inch, 4.6 * inch]),
        p("(~ means the owner's home folder. In Finder: Go > Home. Press Cmd+Shift+. to "
          "show hidden folders.)", SMALL),

        p("Updating", H2),
        p("When a new version arrives as a zip, repeat steps 1 to 3 and choose "
          "<b>Replace</b>. Close the app first (Advanced > Close the app) unless a supplier "
          "run is in progress; in that case wait until it finishes. Markup settings, "
          "caches and earlier upload files are kept."),

        p("Troubleshooting", H2),
        table([
            ["Nothing happens on double-click",
             "The first launch can take a minute or two (step 4). Then open "
             "http://localhost:8642 in a browser. If it still fails, redo step 3, and check "
             "~/SaveBigOnTires/.engine/app_logs/launcher.log."],
            ["'Save Big On Tires is damaged and can't be opened'",
             "macOS says this about unapproved downloads. Move the app to Applications and "
             "redo step 3, or run the xattr command shown there."],
            ["'Bad CPU type' or the app will not start on an older Mac",
             "This is an Intel Mac, or macOS is older than " + min_macos + ". Not supported; "
             "contact the developer."],
            ["A supplier shows 'Running' but nothing is happening",
             "Advanced > Reset stuck status. This happens after a restart or sleep during "
             "a run. Then start that supplier again."],
            ["A supplier run fails at login",
             "Run 1 · Cookies again, then retry. If it keeps failing, the supplier's site "
             "may be down. Try later, and send the log (View log) to the developer."],
            ["Upload says supplier CSVs are 'older than the data they were built from'",
             "Re-run that supplier in 2 · Suppliers (the message names it), then prepare "
             "the upload files again."],
        ], [2.1 * inch, 4.4 * inch]),

        p("Notes for the AI assistant", H2),
        p("• Follow this guide; don't improvise system changes. The app needs no Terminal "
          "work, no Homebrew, no Python and no admin settings beyond steps 2 and 3."),
        p("• Never approve a macOS security prompt, type a password, or confirm a paid "
          "enrichment run on the owner's behalf. Ask the owner to do it."),
        p("• Don't modify or inspect the app's internals or the .engine folder. The app is "
          "compiled, and changes there break updates and support."),
        p("• For anything not covered here, collect the relevant log file and contact the "
          "developer."),
    ]

    doc = SimpleDocTemplate(out, pagesize=letter, leftMargin=0.9 * inch, rightMargin=0.9 * inch,
                            topMargin=0.8 * inch, bottomMargin=0.8 * inch,
                            title="Save Big On Tires — Mac App: Install Instructions",
                            author="Save Big On Tires")

    def footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(MUTED)
        canvas.drawString(0.9 * inch, 0.5 * inch, "Save Big On Tires — Mac App")
        canvas.drawRightString(letter[0] - 0.9 * inch, 0.5 * inch, f"Page {doc_.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--min-macos", required=True)
    args = ap.parse_args()
    build(args.out, args.version, args.min_macos)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
