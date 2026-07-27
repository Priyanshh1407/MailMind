import time
import schedule
import requests
import os
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.columns import Columns
from rich.align import Align
from rich.text import Text
from rich import box

from email_client import authenticate_gmail, get_unread_emails, mark_as_read
from llm_api import classify_email
from notifier import send_telegram_alert
from db_utils import log_email_to_db
import json

# --- CONFIGURATION ---
STATUS_FILE = os.path.join(os.path.dirname(__file__), '..', 'data', 'polling_status.json')

def set_polling_status(is_polling):
    try:
        with open(STATUS_FILE, 'w') as f:
            json.dump({"is_polling": is_polling}, f)
    except:
        pass
# Set to True to see the detailed side-by-side CLI visualization.
# Set to False to run quietly (useful since we have the React Web UI now).
ENABLE_CLI_DASHBOARD = False
# ---------------------

# Initialize the Rich Console
console = Console()

def print_header():
    """Renders a corporate-grade ASCII header."""
    os.system('cls' if os.name == 'nt' else 'clear')
    title = Text("MAILMIND AI", justify="center", style="bold cyan")
    subtitle = Text("Autonomous Priority Routing & Shadow Telemetry", justify="center", style="dim italic")
    header = Panel(
        Text.assemble(title, "\n", subtitle),
        box=box.DOUBLE_EDGE,
        border_style="cyan",
        expand=True
    )
    console.print(header)
    console.print("\n")

def shadow_evaluate_email(subject, body, external_prediction):
    start_time = time.time()
    try:
        response = requests.post(
            "http://127.0.0.1:8000/predict",
            json={"subject": subject, "body": body},
            timeout=3
        )
        if response.status_code == 200:
            local_data = response.json()
            local_label = local_data.get("prediction")
            local_score = local_data.get("confidence_score")
        else:
            local_label, local_score = "API_ERROR", 0.0
    except Exception as e:
        print(f"\n[!] LOCAL MODEL FAILURE: Could not connect to internal AI API.")
        print(f"[!] ERROR DETAILS: {str(e)}\n")
        local_label, local_score = "CONNECTION_FAILED", 0.0
        
    local_latency = round(time.time() - start_time, 3)

    # Build the internal AI table (No printing yet, just building)
    table = Table(show_header=True, header_style="bold magenta", expand=True, box=box.SIMPLE)
    table.add_column("Engine", style="cyan")
    table.add_column("Verdict", justify="center")
    table.add_column("Conf.", justify="center")
    table.add_column("Latency", justify="right")

    ext_color = "green" if external_prediction == "IMPORTANT" else "red" if external_prediction == "ERROR" else "dim"
    loc_color = "green" if local_label == "IMPORTANT" else "red" if local_label in ["API_ERROR", "CONNECTION_FAILED"] else "dim"

    table.add_row("External (Cloud)", f"[{ext_color}]{external_prediction}[/]", "N/A", "N/A")
    table.add_row("MailMind (Local)", f"[{loc_color}]{local_label}[/]", f"{local_score}%", f"{local_latency}s")

    agreement = "✅ SYNCED" if local_label == external_prediction else "⚠️ DESYNC"
    agreement_color = "bold green" if local_label == external_prediction else "bold yellow"
    
    # Wrap the table in a panel to be rendered side-by-side later
    ai_panel = Panel(
        table, 
        title=f"🧠 Neural Telemetry | Status: [{agreement_color}]{agreement}[/]", 
        border_style="magenta",
        width=55
    )
    
    return local_label, ai_panel

def run_agent():
    set_polling_status(True)
    print_header()
    
    gmail_service = authenticate_gmail()
    if not gmail_service:
        console.print("[bold red]Failed to connect to Gmail. Exiting.[/]")
        set_polling_status(False)
        return
    
    with console.status("[bold green]Establishing secure connection and fetching data...", spinner="point"):
        emails = get_unread_emails(gmail_service, max_results=5)

    if not emails:
        console.print(Align.center("[dim italic]No new data packets detected. Engine standing by.[/]"))
        set_polling_status(False)
        return
    
    console.print(f"[bold cyan]Detected {len(emails)} unread packets. Initiating classification matrix...[/]\n")

    for email in emails:
        with console.status(f"[bold yellow]Evaluating Packet ID: {email['id']}...", spinner="arc"):
            # 1. External Call
            decision = classify_email(email['sender'], email['subject'], email['body_snippet'])
            # 2. Local Shadow Call
            local_label, ai_panel = shadow_evaluate_email(email['subject'], email['body_snippet'], decision)

        # Circuit Breaker Logic
        if decision == "ERROR":
            console.print(Panel("[bold red]🚨 CRITICAL: External API failure. Protecting state and halting batch.[/]", border_style="red"))
            break

        # Log to Database
        log_email_to_db(email['id'], email['sender'], email['subject'], email['body_snippet'], decision, local_label)

        if ENABLE_CLI_DASHBOARD:
            # Build Data Panel
            email_text = f"[bold]From:[/bold] {email['sender']}\n[bold]Subject:[/bold] {email['subject']}\n\n[dim]{email['body_snippet'][:80]}...[/dim]"
            border_color = "green" if decision == "IMPORTANT" else "dim"
            data_panel = Panel(email_text, title="📧 Incoming Data", border_style=border_color, width=50)

            # Render Side-by-Side!
            console.print(Columns([data_panel, ai_panel], expand=True))

        # Final Action Footer
        if decision == "IMPORTANT":
            success = send_telegram_alert(email['sender'], email['subject'], email['body_snippet'][:100] + "...")
            alert_status = "[[bold green]✔ Telegram Routed[/]]" if success else "[[bold red]✖ Telegram Failed[/]]"
            console.print(f"   ↳ [bold green]Verdict: PRIORITY[/] {alert_status}")
        elif decision == "UPDATES":
            console.print("   ↳ [bold blue]Verdict: UPDATES[/]")
        else:
            console.print("   ↳ [dim]Verdict: SPAM [[⚪ Suppressed]][/]")

        # Mark as read
        if mark_as_read(gmail_service, email['id']):
            console.print("   ↳ [dim]State: Read[/]\n")

        if ENABLE_CLI_DASHBOARD:
            # Small divider between emails
            console.rule(style="dim", characters="-")
            time.sleep(0.5) # Slight pause for visual cinematic effect
            
    set_polling_status(False)

if __name__ == "__main__":
    console.print(Align.center("\n[bold yellow]Booting up systems. Giving local AI 10 seconds to load...[/]\n"))
    time.sleep(10)
    run_agent()
    
    schedule.every(1).hour.do(run_agent)

    console.print(Align.center("\n[dim]Service active. Polling engine running on 1hr interval. (Ctrl+C to terminate)[/]\n"))

    while True:
        schedule.run_pending()
        time.sleep(1)