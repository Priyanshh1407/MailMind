"""Create original synthetic priority fixtures. No existing data is read."""
import argparse
import json
from pathlib import Path

SCENARIOS = {
'IMPORTANT':[
('Contract signature','Please sign the revised contract before 5 pm today. We cannot start the work without your approval.'),
('Interview confirmation','Your interview is tomorrow morning. Please confirm your attendance and choose the meeting time today.'),
('Invoice correction','The invoice is missing your purchase order. Please send the corrected details today so payment can be released.'),
('Project blocker','The release is blocked on your decision. Please review the two options and reply with approval before noon.'),
('Account protection','A suspicious sign-in was detected. Please review your account and reset your password now if this was not you.'),
('Application deadline','Your application needs one missing document. Upload it by tonight or the application cannot proceed.'),
('Meeting decision','We need your budget decision at the meeting this afternoon. Please prepare your response before joining.'),
('Travel action','Your flight was cancelled. Please select a replacement flight before the rebooking deadline this evening.'),
('Delivery problem','Your parcel cannot be delivered without an address correction. Please confirm the address today.'),
('Access request','A teammate cannot access the repository. Please approve the access request before the deployment starts today.'),
('Review request','Please review the attached design and send comments before tomorrow morning. Your response is needed to continue.'),
('Schedule change','The appointment time has changed. Please confirm the new time today or ask us to reschedule.'),
('Payment action','Your subscription payment failed. Please update your billing method before tonight to avoid service interruption.'),
('Document approval','Please approve the final report today. We are waiting for your signature before sending it to the client.'),
('Support reply','Your support case needs a reply from you. Please send the requested logs today so we can investigate.'),
('Team question','Can you cover the demonstration tomorrow? Please reply today so we can confirm the schedule.'),
('Reservation confirmation','Please confirm your reservation by this afternoon. Unconfirmed reservations will be released.'),
('Personal request','Please call me tonight about the move. I need your answer before I book the transport.'),
('Security incident','An exposed credential needs immediate rotation. Please revoke the key and confirm completion today.'),
('Registration action','Your course registration is incomplete. Choose a session and submit the form before tomorrow.'),
],
'UPDATES':[
('Payment receipt','Your payment was received successfully. This receipt is for your records. No response or further action is required.'),
('Monthly account summary','Here is your monthly account summary. All payments are current. This information does not require a reply.'),
('Release newsletter','Our monthly newsletter describes the latest release and changes. Read it when convenient; no action is needed.'),
('Delivery confirmation','Your package was delivered successfully. This is a delivery confirmation for your records.'),
('Meeting notes','Here are the notes from the completed meeting. They are shared for reference and contain no assigned action for you.'),
('Service maintenance notice','Routine maintenance is planned next month. Your account will remain available and you do not need to reply.'),
('Product information','This optional product guide explains existing features. It is general information without a deadline.'),
('Support resolution','Your support case has been resolved. This message summarizes the fix for your reference; no reply is required.'),
('Travel receipt','Your completed trip receipt is attached for your records. The booking is complete and no changes are needed.'),
('Course resources','The course resource library has new reading material. Access it whenever useful; there is no required task.'),
('Community digest','This weekly community digest contains discussions and optional events. It is not a personal request.'),
('Successful account update','Your requested account update completed successfully. This confirmation requires no further action.'),
('Invoice paid','Your invoice has been paid and closed. Keep this confirmation for your records. No payment is due.'),
('Project progress','The project progress report is shared for information. There are no blockers or decisions awaiting you.'),
('Research digest','This research digest summarizes public articles. Reading is optional and there is no deadline.'),
('Policy information','A policy update takes effect next quarter. This notice is informational and does not require approval.'),
('Optional webinar','A recording of the webinar is now available. Watch it when convenient; attendance and replies are optional.'),
('Subscription confirmation','Your newsletter subscription is confirmed. Future updates will arrive monthly. No response is needed.'),
('Repository summary','This automated weekly repository summary lists completed changes. No review or approval is requested.'),
('Appointment receipt','Your completed appointment receipt is attached. It is provided for your records, without any follow-up task.'),
],
'SPAM':[
('Unsolicited jackpot','You have won a huge cash prize without entering a contest. Send a processing fee now to claim your winnings.'),
('Guaranteed investment','An unsolicited offer guarantees enormous investment returns with no risk. Send funds to secure this secret deal.'),
('Unwanted bulk offer','This unwanted bulk promotion sells miracle products at a secret discount. Buy immediately before the offer disappears.'),
('Fake inheritance','A stranger promises a large inheritance. Share bank details and pay a release fee to receive the money.'),
('Miracle treatment','This unsolicited advertisement claims a miracle treatment solves every condition. Buy today with a special secret code.'),
('Phishing verification','A suspicious unknown sender asks for your password and banking PIN to avoid account deletion. Send the details directly.'),
('Fake job fee','A stranger guarantees a job without an interview. Pay an upfront registration fee to receive the offer.'),
('Unwanted coupon','An unsolicited mass advertisement offers random coupons for products you never requested. Purchase now.'),
('Fake delivery fee','A suspicious unknown link requests your full card details to pay an unexpected delivery release fee.'),
('Lottery membership','An unwanted lottery advertisement promises certain winnings if you pay for a premium membership.'),
('Secret cryptocurrency','An unsolicited crypto scheme promises to multiply every deposit. Transfer money immediately to a stranger.'),
('Follower promotion','An unwanted bulk promotion sells fake followers and engagement. Buy a package through an unknown link.'),
('Unrequested loan','A stranger offers an instant loan and demands an advance fee before providing any terms.'),
('Password collection','A suspicious message asks you to email your password to an unknown address to verify a fake security alert.'),
('Counterfeit sale','An unsolicited bulk advertisement promotes counterfeit luxury goods with impossible discounts.'),
('Prize verification fee','You supposedly won a prize you never entered. Pay a verification fee and send identity documents to a stranger.'),
('Fake tax refund','A suspicious unknown sender promises an unexpected tax refund after you submit banking credentials.'),
('Unwanted gambling','This unsolicited gambling promotion claims guaranteed wins and asks you to deposit money immediately.'),
('Fake invoice attachment','An unknown sender demands payment for an order you never placed and asks you to open a suspicious attachment.'),
('Bulk marketing blast','An unwanted mass marketing message repeats irrelevant advertisements and pressure to buy an unrequested service.'),
]}


def build(output):
    output=Path(output)
    output.mkdir(parents=True,exist_ok=True)
    rows=[]
    for label,scenarios in SCENARIOS.items():
        for number,(subject,body) in enumerate(scenarios):
            group=f'{label.lower()}-{number:02d}'
            for variant in range(3):
                prefixes=['Hello. ','A message for your inbox: ','For your attention: ']
                rows.append({'id':group+f'-{variant}','group_id':group,'thread_id':group,'template_id':group,
                    'subject':subject+['',' — details',' — message'][variant],
                    'body':prefixes[variant]+body,'human_label':label,
                    'annotation_basis':'Scenario semantics: explicit direct action, legitimate nonurgent information, or unwanted/deceptive content.'})
    manifest={'dataset_id':'mailmind-synthetic-priority-v1','source':'Original scenario-based fixtures authored for this project; no real emails or third-party dataset copied.',
              'license':'CC0-1.0','license_url':'https://creativecommons.org/publicdomain/zero/1.0/',
              'task':'three_category_priority','synthetic':True,
              'label_semantics':{'IMPORTANT':'Direct legitimate action or attention needed, including deadlines.',
                                 'UPDATES':'Legitimate useful nonurgent information with no required response.',
                                 'SPAM':'Unwanted bulk junk, deceptive offers or phishing.'},
              'limitations':['Small hand-designed synthetic benchmark; not independently human-reviewed real inbox data.',
                             'Wording is more explicit than real mail. English only. No population accuracy claim.',
                             'Template/thread variants must stay in the same split.']}
    (output/'emails.json').write_text(json.dumps(rows,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    (output/'provenance.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    (output/'LICENSE.txt').write_text('Original synthetic fixtures are dedicated under CC0 1.0.\nhttps://creativecommons.org/publicdomain/zero/1.0/\n',encoding='utf-8')
    return rows,manifest

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',default='fixtures/priority_benchmark')
    args=parser.parse_args();rows,_=build(args.output)
    print(f'Created {len(rows)} synthetic messages in {args.output}')
