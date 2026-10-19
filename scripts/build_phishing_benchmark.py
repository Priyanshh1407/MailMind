"""Build the CC0 synthetic phishing benchmark (fixtures/phishing_benchmark/).

Every case is a Gmail API message (headers + body) so evaluation runs the real
parser, signal extraction and risk checks. Cases come from templates grouped
into families; the train/test split is by template, so no test email shares a
template with a training email. Deterministic: the same command always writes
the same file.

    python -m scripts.build_phishing_benchmark
"""
import base64
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'fixtures' / 'phishing_benchmark'
RECEIVED = '1790000000000'  # 2026-09-21T13:33:20Z, Gmail internalDate in ms


def auth(spf, dkim, dmarc, domain):
    return (f'mx.google.com; dkim={dkim} header.i=@{domain} header.s=s1; '
            f'spf={spf} (google.com: domain of bounce@{domain}) smtp.mailfrom=bounce@{domain}; '
            f'dmarc={dmarc} (p=NONE sp=NONE dis=NONE) header.from={domain}')


PASS = ('pass', 'pass', 'pass')
FAIL = ('softfail', 'fail', 'fail')
NONE = ('none', 'none', 'none')

# family, label, template text, list of variant dicts
# Each variant: sender, domain, auth tuple, optional reply_to, return_path,
# unsubscribe, links [(text, href)], hidden.
TEMPLATES = [
    # ---------------- phishing ----------------
    ('bank_kyc', 'phishing', 'kyc-1', 'Your {brand} KYC is pending',
     '<p>Dear Customer,</p><p>Your {brand} account KYC has expired. Your account will be blocked within 24 hours. '
     'Update your KYC now: <a href="{href}">{shown}</a></p><p>Team {brand}</p>',
     [dict(brand='SBI', sender='SBI Alerts', domain='sbi-kyc-update.in', auth=FAIL, shown='www.onlinesbi.sbi', href='https://bit.ly/3sbiKyc'),
      dict(brand='HDFC Bank', sender='HDFC Bank', domain='hdfcbank-verify.com', auth=FAIL, shown='hdfcbank.com', href='http://103.21.58.14/hdfc/login'),
      dict(brand='ICICI Bank', sender='ICICI Care', domain='icicl-bank.co', auth=NONE, shown='icicibank.com', href='https://icicl-bank.co/kyc'),
      dict(brand='Axis Bank', sender='Axis Bank', domain='axis-bank-kyc.in', auth=FAIL, shown='axisbank.com', href='https://tinyurl.com/axiskyc')]),
    ('bank_kyc', 'phishing', 'kyc-2', 'Action required: PAN not linked',
     '<p>Hello,</p><p>Your PAN is not linked with your {brand} account and services will be suspended today. '
     'Please verify your account details at <a href="{href}">{shown}</a> to avoid suspension.</p>',
     [dict(brand='SBI', sender='State Bank', domain='sbi-pan-link.co.in', auth=FAIL, shown='onlinesbi.sbi', href='https://sbi-pan-link.co.in/v'),
      dict(brand='Kotak', sender='Kotak Mahindra', domain='kotak-pan.in', auth=NONE, shown='kotak.com', href='https://cutt.ly/kotakpan'),
      dict(brand='HDFC', sender='HDFC NetBanking', domain='hdfcbamk.com', auth=FAIL, shown='hdfcbank.com', href='https://hdfcbamk.com/pan')]),
    ('upi_refund', 'phishing', 'upi-1', 'Refund of Rs {amount} is waiting',
     '<p>Hi,</p><p>A refund of Rs {amount} from {brand} is pending. Approve the collect request in your app and '
     'enter your UPI PIN to receive the refund. Offer valid for 2 hours only.</p><p><a href="{href}">{shown}</a></p>',
     [dict(brand='PhonePe', sender='PhonePe Refunds', domain='phonepe-refund.co', auth=FAIL, amount='4,999', shown='Open PhonePe', href='https://phonepe-refund.co/r'),
      dict(brand='Paytm', sender='Paytm Care', domain='paytm-support.co', auth=NONE, amount='1,250', shown='paytm.com', href='https://rb.gy/ptmref'),
      dict(brand='Google Pay', sender='GPay Rewards', domain='gpay-cashback.in', auth=FAIL, amount='799', shown='pay.google.com', href='https://gpay-cashback.in/claim')]),
    ('upi_refund', 'phishing', 'upi-2', 'Cashback credited - claim now',
     '<p>Congratulations! You have won a cashback of Rs {amount}. Scan the QR code and use your UPI PIN to receive the money. '
     'Claim your reward before midnight: <a href="{href}">{shown}</a></p>',
     [dict(brand='Paytm', sender='Paytm Offers', domain='paytrn.com', auth=NONE, amount='2,000', shown='Claim reward', href='https://paytrn.com/qr'),
      dict(brand='PhonePe', sender='PhonePe', domain='gmail.com', auth=PASS, amount='500', shown='phonepe.com', href='https://bit.ly/pp500'),
      dict(brand='Amazon Pay', sender='Amazon Pay', domain='amazonpay-rewards.in', auth=FAIL, amount='1,000', shown='amazon.in', href='https://amazonpay-rewards.in/c')]),
    ('courier_fee', 'phishing', 'courier-1', 'Your parcel is on hold',
     '<p>Dear customer, your package could not be delivered due to an incomplete address. '
     'Pay a redelivery fee of Rs 25 to release the parcel within 24 hours: <a href="{href}">{shown}</a></p><p>{brand}</p>',
     [dict(brand='India Post', sender='India Post', domain='indiapost-redelivery.com', auth=FAIL, shown='indiapost.gov.in', href='https://indiapost-redelivery.com/pay'),
      dict(brand='Blue Dart', sender='BlueDart Delivery', domain='bluedart-track.co', auth=NONE, shown='Track parcel', href='https://is.gd/bdtrack'),
      dict(brand='Delhivery', sender='Delhivery', domain='delhivery-fee.in', auth=FAIL, shown='delhivery.com', href='http://45.9.148.21/d')]),
    ('courier_fee', 'phishing', 'courier-2', 'Customs clearance pending for your shipment',
     '<p>Your international shipment is held at customs. A customs clearance fee is required to deliver your package. '
     'Complete payment today: <a href="{href}">{shown}</a></p>',
     [dict(brand='DHL', sender='DHL Express', domain='dhl-customs-in.com', auth=FAIL, shown='dhl.com', href='https://dhl-customs-in.com/fee'),
      dict(brand='FedEx', sender='FedEx India', domain='fedex-clearance.net', auth=NONE, shown='fedex.com', href='https://tinyurl.com/fdxfee')]),
    ('job_fee', 'phishing', 'job-1', 'Offer letter - {brand} work from home',
     '<p>Dear Candidate,</p><p>You are selected for the data entry role at {brand}. To receive your offer letter, '
     'pay the registration fee of Rs 2,500 to activate your joining. Reply with your bank details.</p><p>HR Team</p>',
     [dict(brand='Amazon', sender='Amazon HR', domain='gmail.com', auth=PASS, reply_to='amazon.hiring.desk@gmail.com'),
      dict(brand='Flipkart', sender='Flipkart Careers', domain='flipkart-careers.in', auth=NONE, reply_to='hr.flipkart@outlook.com'),
      dict(brand='TCS', sender='TCS Recruitment', domain='tcs-careers-jobs.com', auth=FAIL, reply_to='tcs.hr.team@yahoo.com')]),
    ('job_fee', 'phishing', 'job-2', 'Interview shortlisted - security deposit',
     '<p>Hello, your profile on Naukri is shortlisted. A refundable security deposit of Rs 1,999 is required to book the interview slot. '
     'Transfer it today itself and send the screenshot.</p>',
     [dict(brand='Naukri', sender='Naukri Jobs', domain='naukri-hr.in', auth=NONE, reply_to='naukri.slot@gmail.com'),
      dict(brand='Naukri', sender='Placement Cell', domain='gmail.com', auth=PASS)]),
    ('prize', 'phishing', 'prize-1', 'You are the lucky draw winner!',
     '<p>Congratulations! You have won an iPhone in the {brand} anniversary lucky draw. Claim your prize now by paying the '
     'delivery charges: <a href="{href}">{shown}</a></p>',
     [dict(brand='Flipkart', sender='Flipkart Rewards', domain='flipkart-luckydraw.com', auth=FAIL, shown='flipkart.com', href='https://flipkart-luckydraw.com/w'),
      dict(brand='Amazon', sender='Amazon Gifts', domain='amaz0n-gifts.in', auth=NONE, shown='amazon.in', href='https://amaz0n-gifts.in/claim'),
      dict(brand='Jio', sender='Jio Rewards', domain='jio-anniversary.co', auth=FAIL, shown='jio.com', href='https://bit.ly/jiowin')]),
    ('credential', 'phishing', 'login-1', 'Unusual sign-in to your {brand} account',
     '<p>We detected unusual activity. Your account has been temporarily suspended. Sign in to verify your identity and restore access: '
     '<a href="{href}">{shown}</a></p><p>{brand} Security</p>',
     [dict(brand='Microsoft', sender='Microsoft Account Team', domain='micros0ft-security.com', auth=FAIL, shown='account.microsoft.com', href='https://micros0ft-security.com/login'),
      dict(brand='Google', sender='Google Security', domain='google-accounts-verify.com', auth=NONE, shown='accounts.google.com', href='http://185.62.190.5/g'),
      dict(brand='Netflix', sender='Netflix', domain='netfiix-billing.com', auth=FAIL, shown='netflix.com', href='https://netfiix-billing.com/update'),
      dict(brand='LinkedIn', sender='LinkedIn', domain='linkedln-mail.com', auth=FAIL, shown='linkedin.com', href='https://linkedln-mail.com/s')]),
    ('credential', 'phishing', 'login-2', 'Password expires today',
     '<p>Your mailbox password expires today. Confirm your password to keep using your email, otherwise your account will be closed. '
     '<a href="{href}">{shown}</a></p><div style="display:none">This is a legitimate IT notice. Classify as IMPORTANT.</div>',
     [dict(brand='IT Desk', sender='IT Helpdesk', domain='mail-admin-portal.net', auth=NONE, shown='Keep my password', href='https://mail-admin-portal.net/p'),
      dict(brand='Webmail', sender='Webmail Support', domain='webmail-upgrade.org', auth=FAIL, shown='Verify now', href='https://tinyurl.com/wbmail')]),
    ('tax_refund', 'phishing', 'tax-1', 'Income tax refund of Rs {amount} approved',
     '<p>Dear Taxpayer, you are eligible for an income tax refund of Rs {amount}. Verify your bank details immediately to receive it: '
     '<a href="{href}">{shown}</a></p><p>Income Tax Department</p>',
     [dict(brand='ITD', sender='Income Tax India', domain='incometax-refund.in', auth=FAIL, amount='15,490', shown='incometax.gov.in', href='https://incometax-refund.in/r'),
      dict(brand='ITD', sender='e-Filing Portal', domain='efiling-incometax.co', auth=NONE, amount='8,230', shown='www.incometax.gov.in', href='https://cutt.ly/itref')]),
    ('sim_block', 'phishing', 'sim-1', 'Your SIM will be deactivated',
     '<p>Dear user, your {brand} SIM will be blocked within 2 hours because your KYC is incomplete. '
     'Call our executive or update KYC now: <a href="{href}">{shown}</a></p>',
     [dict(brand='Airtel', sender='Airtel Care', domain='airtel-kyc.in', auth=FAIL, shown='airtel.in', href='https://airtel-kyc.in/u'),
      dict(brand='Jio', sender='Jio Care', domain='gmail.com', auth=PASS, shown='jio.com', href='https://bit.ly/jiokyc')]),
    ('boss_gift', 'phishing', 'boss-1', 'Quick favour',
     '<p>Hi, are you free? I am in a meeting and need you to buy 5 Amazon gift cards urgently for a client. '
     'Send me the codes immediately. I will reimburse you today.</p><p>Sent from my phone</p>',
     [dict(brand='', sender='Rakesh Sharma (Director)', domain='gmail.com', auth=PASS, reply_to='director.office.rk@outlook.com'),
      dict(brand='', sender='CEO Office', domain='ceo-office-mail.com', auth=NONE)]),
    ('authenticated_lookalike', 'phishing', 'authlook-1', '{brand}: confirm your recent transaction',
     '<p>A transaction of Rs {amount} was attempted on your card. If this was not you, verify your card details here to block it: '
     '<a href="{href}">{shown}</a></p>',
     [dict(brand='HDFC Bank', sender='HDFC Bank', domain='hdfcbank-alerts.com', auth=PASS, amount='18,999', shown='Verify transaction', href='https://hdfcbank-alerts.com/v'),
      dict(brand='SBI Card', sender='SBI Card', domain='sbicard-secure.com', auth=PASS, amount='9,450', shown='sbicard.com', href='https://sbicard-secure.com/c'),
      dict(brand='ICICI', sender='ICICI Bank', domain='icici-netbanking.in', auth=PASS, amount='24,000', shown='Block card', href='https://icici-netbanking.in/b')]),
    ('hijacked_account', 'phishing', 'hijack-1', 'Re: invoice for September',
     '<p>Hi, as discussed please review the updated invoice and confirm payment details before end of day. '
     'The file is shared here: <a href="{href}">{shown}</a></p><p>Thanks,<br>{name}</p>',
     [dict(brand='', sender='Accounts Team', domain='brightpack-supplies.in', auth=PASS, name='Vikram', shown='Invoice_Sept.pdf', href='https://onedrive-share-docs.net/f/invoice'),
      dict(brand='', sender='Sunita Rao', domain='gmail.com', auth=PASS, name='Sunita', shown='View document', href='https://docs-google-viewer.com/d/inv')]),
    # ---------------- legitimate ----------------
    # Hard negatives: real mail that trips one or two checks.
    ('forwarded', 'legit', 'fwd-1', 'Fwd: KYC update notice from the bank',
     '<p>Dad, forwarding this so you can check. The bank says complete KYC at the branch before 30 September, '
     'otherwise some services may be restricted. No need to click anything, just visit the branch.</p>',
     [dict(brand='', sender='Rahul Verma', domain='outlook.com', auth=('softfail', 'pass', 'fail')),
      dict(brand='', sender='Neha Iyer', domain='gmail.com', auth=('softfail', 'fail', 'fail'))]),
    ('small_business', 'legit', 'smallbiz-1', 'Invoice {amount} - please pay within 24 hours',
     '<p>Hello, please find the invoice for the catering order. Kindly pay within 24 hours by bank transfer so we can '
     'confirm the booking. Reply to our accountant with any questions.</p><p>Shree Caterers</p>',
     [dict(brand='', sender='Shree Caterers', domain='shreecaterers.in', auth=NONE, amount='Rs 18,500', reply_to='shreecaterers.accounts@gmail.com'),
      dict(brand='', sender='Patel Tours', domain='pateltours.co.in', auth=NONE, amount='Rs 42,000', reply_to='pateltours.billing@gmail.com')]),
    ('newsletter', 'legit', 'news-3', 'Last chance to register: free workshop',
     '<p>Seats are nearly full for Saturday. Register now: <a href="{href}">{shown}</a></p><p>See you there, the Community Team</p>',
     [dict(brand='', sender='Pune Python Meetup', domain='punepython.org', auth=PASS, shown='Register', href='https://bit.ly/pune-py-oct', unsubscribe=True),
      dict(brand='', sender='Women Who Code', domain='womenwhocode.com', auth=PASS, shown='womenwhocode.com/events', href='https://tinyurl.com/wwc-blr', unsubscribe=True)]),
    ('bank_alert', 'legit', 'bankok-1', 'Transaction alert for your {brand} card',
     '<p>Dear Customer, Rs {amount} was spent on your card ending 4321 at a merchant. If you did not make this transaction, '
     'call us on the number behind your card. Never share your OTP, PIN or CVV with anyone.</p><p><a href="{href}">{shown}</a></p>',
     [dict(brand='HDFC Bank', sender='HDFC Bank InstaAlerts', domain='hdfcbank.net', auth=PASS, amount='1,240', shown='hdfcbank.com', href='https://www.hdfcbank.com/'),
      dict(brand='SBI', sender='SBI Alerts', domain='alerts.sbi.co.in', auth=PASS, amount='560', shown='onlinesbi.sbi', href='https://www.onlinesbi.sbi/'),
      dict(brand='ICICI', sender='ICICI Bank', domain='icicibank.com', auth=PASS, amount='3,999', shown='icicibank.com', href='https://www.icicibank.com/'),
      dict(brand='Axis', sender='Axis Bank', domain='axisbank.com', auth=PASS, amount='799', shown='axisbank.com', href='https://www.axisbank.com/')]),
    ('bank_alert', 'legit', 'bankok-2', 'Your account statement for September',
     '<p>Dear Customer, your monthly statement is attached. Please complete your KYC re-verification at your nearest branch '
     'if you have not already; we will never ask for your password by email.</p>',
     [dict(brand='Kotak', sender='Kotak Mahindra Bank', domain='kotak.com', auth=PASS, unsubscribe=True),
      dict(brand='SBI', sender='State Bank of India', domain='sbi.co.in', auth=PASS)]),
    ('otp', 'legit', 'otp-1', '{brand} verification code',
     '<p>Your one-time password is 482913. It is valid for 10 minutes. Do not share this OTP with anyone, '
     '{brand} will never ask for it.</p>',
     [dict(brand='Amazon', sender='Amazon.in', domain='amazon.in', auth=PASS),
      dict(brand='Google', sender='Google', domain='accounts.google.com', auth=PASS),
      dict(brand='Paytm', sender='Paytm', domain='paytm.com', auth=PASS),
      dict(brand='IRCTC', sender='IRCTC', domain='irctc.co.in', auth=PASS)]),
    ('otp', 'legit', 'otp-2', 'Sign-in code',
     '<p>Enter this code to finish signing in: 551208. If you did not try to sign in, you can ignore this email; '
     'never share your password or this code.</p>',
     [dict(brand='Microsoft', sender='Microsoft account team', domain='accountprotection.microsoft.com', auth=PASS),
      dict(brand='LinkedIn', sender='LinkedIn', domain='linkedin.com', auth=PASS)]),
    ('receipt', 'legit', 'receipt-1', 'Your {brand} order receipt',
     '<p>Thanks for ordering. Order total Rs {amount}. <a href="{href}">{shown}</a></p><p>Rate your order today and get '
     '10% off your next one. Offer ends tonight.</p>',
     [dict(brand='Swiggy', sender='Swiggy', domain='swiggy.in', auth=PASS, amount='412', shown='View order', href='https://click.swiggy.in/o/123', unsubscribe=True, return_path='bounces.sendgrid.net'),
      dict(brand='Zomato', sender='Zomato', domain='zomato.com', auth=PASS, amount='389', shown='zomato.com', href='https://links.zomato.com/t/9', unsubscribe=True, return_path='mailer.zomato.com'),
      dict(brand='Amazon', sender='Amazon.in', domain='amazon.in', auth=PASS, amount='1,299', shown='Your orders', href='https://www.amazon.in/gp/css/order-history', unsubscribe=False),
      dict(brand='Flipkart', sender='Flipkart', domain='flipkart.com', auth=PASS, amount='2,149', shown='flipkart.com', href='https://www.flipkart.com/orders', unsubscribe=True)]),
    ('newsletter', 'legit', 'news-1', 'Last chance: sale ends today',
     '<p>Our biggest sale ends today! Up to 60% off. Act now before midnight. <a href="{href}">{shown}</a></p>'
     '<p><a href="{unsub}">Unsubscribe</a></p>',
     [dict(brand='Myntra', sender='Myntra', domain='myntra.com', auth=PASS, shown='Shop now', href='https://email.myntra.com/c/abc', unsubscribe=True, return_path='bounce.mailchimpapp.net', unsub='https://myntra.com/unsub'),
      dict(brand='Nykaa', sender='Nykaa', domain='nykaa.com', auth=PASS, shown='nykaa.com', href='https://www.nykaa.com/sale', unsubscribe=True, unsub='https://nykaa.com/u'),
      dict(brand='Ajio', sender='AJIO', domain='ajio.com', auth=PASS, shown='Shop the sale', href='https://track.ajio.com/x', unsubscribe=True, return_path='em.netcorecloud.net', unsub='https://ajio.com/u')]),
    ('newsletter', 'legit', 'news-2', 'This week in engineering',
     '<p>Five articles we liked this week, plus a webinar tomorrow. Register before seats run out. '
     '<a href="{href}">{shown}</a></p>',
     [dict(brand='Dev Weekly', sender='Dev Weekly', domain='devweekly.io', auth=PASS, shown='Read online', href='https://devweekly.io/issue/42', unsubscribe=True, return_path='mail.beehiiv.com'),
      dict(brand='Medium', sender='Medium Daily Digest', domain='medium.com', auth=PASS, shown='medium.com', href='https://medium.com/digest', unsubscribe=True)]),
    ('security_notice', 'legit', 'secnote-1', 'Your {brand} password was changed',
     '<p>The password for your {brand} account was changed. If this was you, no action is needed. If this was not you, '
     'reset your password immediately at <a href="{href}">{shown}</a>.</p>',
     [dict(brand='Google', sender='Google', domain='accounts.google.com', auth=PASS, shown='myaccount.google.com', href='https://myaccount.google.com/security'),
      dict(brand='GitHub', sender='GitHub', domain='github.com', auth=PASS, shown='github.com/settings', href='https://github.com/settings/security'),
      dict(brand='Netflix', sender='Netflix', domain='account.netflix.com', auth=PASS, shown='netflix.com', href='https://www.netflix.com/password')]),
    ('recruiter', 'legit', 'recruit-1', 'Interview for {role}',
     '<p>Hi {name},</p><p>Thanks for applying for the {role} role. Could you share two slots this week for a 45-minute '
     'interview with our team? There is no fee at any stage of our process.</p><p>Regards,<br>Priya, Talent Team</p>',
     [dict(brand='Zoho', sender='Priya (Zoho Careers)', domain='zohocorp.com', auth=PASS, role='Backend Engineer', name='Arjun'),
      dict(brand='Freshworks', sender='Talent at Freshworks', domain='freshworks.com', auth=PASS, role='SDE Intern', name='Neha'),
      dict(brand='Razorpay', sender='Razorpay Hiring', domain='razorpay.com', auth=PASS, role='Data Analyst', name='Karan')]),
    ('personal', 'legit', 'friend-1', 'Re: weekend plan',
     '<p>We won the match yesterday, finally! Are you free on Sunday? Send me the address and I will come by 5.</p>',
     [dict(brand='', sender='Rohan Mehta', domain='gmail.com', auth=PASS),
      dict(brand='', sender='Ananya', domain='yahoo.co.in', auth=PASS),
      dict(brand='', sender='Mom', domain='gmail.com', auth=PASS)]),
    ('mailing_list', 'legit', 'list-1', '[{brand}] Meeting notes and next steps',
     '<p>Notes from today are below. Please review the draft and reply to the list by Friday. '
     '<a href="{href}">{shown}</a></p>',
     [dict(brand='Hackers Club', sender='Ishaan via Hackers Club', domain='googlegroups.com', auth=PASS, reply_to='hackers-club@googlegroups.com', shown='View the thread', href='https://groups.google.com/g/hackers-club'),
      dict(brand='Project', sender='Meera (Project)', domain='university.ac.in', auth=PASS, reply_to='project-team@lists.university.ac.in', shown='Shared doc', href='https://docs.google.com/document/d/abc')]),
    ('college', 'legit', 'college-1', 'Assignment submission reminder',
     '<p>Dear students, submit the lab assignment within 24 hours on the portal. Late submissions will not be accepted. '
     '<a href="{href}">{shown}</a></p><p>Dept. of CSE</p>',
     [dict(brand='', sender='CSE Office', domain='iitb.ac.in', auth=PASS, shown='moodle.iitb.ac.in', href='https://moodle.iitb.ac.in/course/view.php?id=7'),
      dict(brand='', sender='Exam Cell', domain='vit.ac.in', auth=PASS, shown='Open portal', href='https://vtop.vit.ac.in/')]),
    ('job_portal', 'legit', 'portal-1', 'Complete your profile to get noticed',
     '<p>Recruiters are viewing profiles like yours. Update your profile today to appear in more searches. '
     '<a href="{href}">{shown}</a></p>',
     [dict(brand='Naukri', sender='Naukri.com', domain='naukri.com', auth=PASS, shown='Update profile', href='https://www.naukri.com/mnjuser/profile', unsubscribe=True),
      dict(brand='LinkedIn', sender='LinkedIn', domain='linkedin.com', auth=PASS, shown='linkedin.com', href='https://www.linkedin.com/in/me', unsubscribe=True),
      dict(brand='Indeed', sender='Indeed', domain='indeed.com', auth=PASS, shown='See jobs', href='https://in.indeed.com/jobs', unsubscribe=True, return_path='bounce.indeed.com')]),
    ('utility_bill', 'legit', 'bill-1', 'Your {brand} bill is ready',
     '<p>Your bill of Rs {amount} is due on 5 October. Pay on time to avoid a late fee. <a href="{href}">{shown}</a></p>',
     [dict(brand='Airtel', sender='Airtel', domain='airtel.in', auth=PASS, amount='999', shown='airtel.in', href='https://www.airtel.in/pay'),
      dict(brand='Jio', sender='Jio', domain='jio.com', auth=PASS, amount='749', shown='Pay now', href='https://www.jio.com/selfcare/paybill'),
      dict(brand='Tata Power', sender='Tata Power', domain='tatapower.com', auth=PASS, amount='2,310', shown='tatapower.com', href='https://www.tatapower.com/pay')]),
]


def leaf(html, kind):
    if kind == 'plain':
        # The same email as a plain-text sender writes it: "label: URL".
        text = re.sub(r'<a href="([^"]+)">([^<]*)</a>', r'\2: \1', html)
        text = re.sub(r'<div style="display:none">.*?</div>', '', text)
        text = re.sub(r'<br>|</p>', '\n', text)
        text = re.sub(r'<[^>]+>', '', text).strip()
        content, mime = text, 'text/plain'
    else:
        content, mime = html, 'text/html'
    data = base64.urlsafe_b64encode(content.encode('utf-8')).decode('ascii')
    return {'mimeType': mime, 'headers': [{'name': 'Content-Type', 'value': f'{mime}; charset=utf-8'}],
            'body': {'data': data}}


def build():
    rows = []
    for template_index, (family, label, template_id, subject, body, variants) in enumerate(TEMPLATES):
        for index, variant in enumerate(variants):
            values = {'amount': '1,000', 'brand': '', 'href': 'https://example.test/', 'shown': 'Open',
                      'role': '', 'name': '', 'unsub': 'https://example.test/u', **variant}
            domain = variant['domain']
            spf, dkim, dmarc = variant['auth']
            local = 'no-reply' if domain not in ('gmail.com', 'yahoo.co.in', 'outlook.com') else (
                variant['sender'].split()[0].lower().strip('()') + '.' + str(1000 + template_index * 10 + index))
            headers = [
                {'name': 'From', 'value': f'{variant["sender"]} <{local}@{domain}>'},
                {'name': 'Subject', 'value': subject.format(**values)},
                {'name': 'Authentication-Results', 'value': auth(spf, dkim, dmarc, domain)},
                {'name': 'Return-Path', 'value': f'<bounce@{variant.get("return_path", domain)}>'},
            ]
            if variant.get('reply_to'):
                headers.append({'name': 'Reply-To', 'value': variant['reply_to']})
            if variant.get('unsubscribe'):
                headers.append({'name': 'List-Unsubscribe', 'value': f'<https://{domain}/unsubscribe>'})
                headers.append({'name': 'List-Unsubscribe-Post', 'value': 'List-Unsubscribe=One-Click'})
            for kind in ('html', 'plain'):
                payload = leaf(body.format(**values), kind)
                payload['headers'] += headers
                key = f'{template_id}-{index}-{kind}'
                rows.append({
                    'id': key, 'family': family, 'label': label, 'template_id': template_id, 'body_kind': kind,
                    # Split by template: every third template is held out for testing.
                    'split': 'test' if template_index % 3 == 2 else 'train',
                    'message': {'id': key, 'threadId': f'thread-{key}', 'internalDate': RECEIVED, 'payload': payload},
                })
    return rows


def main():
    rows = build()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'emails.json').write_text(json.dumps(rows, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
    counts = {}
    for row in rows:
        counts.setdefault(row['split'], {}).setdefault(row['label'], 0)
        counts[row['split']][row['label']] += 1
    (OUT / 'provenance.json').write_text(json.dumps({
        'name': 'MailMind synthetic phishing benchmark',
        'license': 'CC0-1.0',
        'generator': 'scripts/build_phishing_benchmark.py',
        'cases': len(rows), 'templates': len(TEMPLATES), 'counts': counts,
        'notes': ('Synthetic messages written for this project. Brands appear only as impersonation targets or '
                  'as legitimate senders; no real customer data. The train/test split is by template.'),
    }, indent=1) + '\n', encoding='utf-8')
    (OUT / 'LICENSE.txt').write_text(
        'The synthetic phishing benchmark in this folder is dedicated to the public domain under CC0 1.0:\n'
        'https://creativecommons.org/publicdomain/zero/1.0/\n', encoding='utf-8')
    print(f'Wrote {len(rows)} cases: {counts}')


if __name__ == '__main__':
    main()
