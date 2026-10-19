"""Build the CC0 synthetic receipts benchmark (fixtures/receipts_benchmark/).

Each case is an email (sender, subject, body, received date) with its gold
answer: is it a transaction, and if so its kind, total, currency and
merchant. The merchant is whoever was paid (or refunded) when the email names
them, such as the shop in a UPI or card alert; otherwise the sender. Traps sit next to the real total: discounts, "you saved",
available balance, MRP, cashback and delivery fees. Dated series of charges
test subscription detection, including irregular repeat purchases that are
not subscriptions. Deterministic.

    python -m scripts.build_receipts_benchmark
"""
from datetime import date, timedelta
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'fixtures' / 'receipts_benchmark'


def rupees(minor):
    whole, paise = divmod(minor, 100)
    text = f'{whole:,}'
    if whole >= 100000:  # Indian grouping: 1,23,456
        head, tail = str(whole)[:-3], str(whole)[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        text = ','.join([head, *groups, tail]) if head else ','.join([*groups, tail])
    return text + (f'.{paise:02d}' if paise else '')


FOOD = [('Swiggy', 'noreply@swiggy.in', 'Swiggy'), ('Zomato', 'noreply@zomato.com', 'Zomato')]
SHOPS = [('Amazon.in', 'auto-confirm@amazon.in', 'Amazon'), ('Flipkart', 'no-reply@flipkart.com', 'Flipkart'),
         ('Myntra', 'orders@myntra.com', 'Myntra'), ('Nykaa', 'orders@nykaa.com', 'Nykaa')]
BANKS = [('HDFC Bank InstaAlerts', 'alerts@hdfcbank.net', 'HDFC Bank'), ('ICICI Bank', 'alerts@icicibank.com', 'ICICI Bank'),
         ('SBI', 'alerts@sbi.co.in', 'SBI')]


def cases():
    rows = []
    day = date(2026, 7, 1)

    def add(case_id, sender, subject, body, received, gold, series=None):
        rows.append({'id': case_id, 'sender': sender, 'subject': subject, 'body': body,
                     'received': received.isoformat(), 'gold': gold, **({'series': series} if series else {})})

    # Food delivery: item total, fees, discount, then the order total.
    for index, (name, address, merchant) in enumerate(FOOD * 5):
        item, fee, off = 28000 + index * 1350, 3500 + index * 100, 5000 + index * 200
        total = item + fee - off
        add(f'food-{index}', f'{name} <{address}>', f'Your {name} order is delivered',
            f'Thanks for ordering!\nItem total: Rs {rupees(item)}\nDelivery fee: Rs {rupees(fee)}\n'
            f'Discount: -Rs {rupees(off)}\nOrder total: Rs {rupees(total)}\nYou saved Rs {rupees(off)} on this order.',
            day + timedelta(days=index * 3), {'kind': 'purchase', 'amount_minor': total, 'currency': 'INR', 'merchant': merchant})
    # Shopping: MRP and "you saved" around the order total.
    for index, (name, address, merchant) in enumerate(SHOPS * 4):
        mrp, total = 249900 + index * 5000, 129900 + index * 3100
        add(f'shop-{index}', f'{name} <{address}>', f'Order confirmed: #{40211 + index}',
            f'Hello, your order has been placed.\nMRP: ₹{rupees(mrp)}\nGrand Total: ₹{rupees(total)}\n'
            f'You saved ₹{rupees(mrp - total)}. Cashback of ₹50 will be credited later.',
            day + timedelta(days=2 + index * 4), {'kind': 'purchase', 'amount_minor': total, 'currency': 'INR', 'merchant': merchant})
    # Card and account debits: available balance is not the amount.
    for index, (name, address, merchant) in enumerate(BANKS * 4):
        spent, balance = 250000 + index * 12345, 4523000 + index * 99999
        add(f'debit-{index}', f'{name} <{address}>', 'Transaction alert',
            f'Dear Customer, Rs.{rupees(spent)} has been debited from your account ending 4821 on '
            f'{(day + timedelta(days=index)).strftime("%d-%m-%Y")}. Available balance: Rs.{rupees(balance)}.',
            day + timedelta(days=index * 5), {'kind': 'debit', 'amount_minor': spent, 'currency': 'INR', 'merchant': merchant})
    # UPI payments to people and shops.
    for index, payee in enumerate(['Ramesh Kirana Store', 'Sharma Medicals', 'Blue Tokai Coffee', 'Auto fare', 'Gupta Dairy',
                                   'Fresh Fruits Mart']):
        amount = 12000 + index * 4750
        add(f'upi-{index}', 'PhonePe <noreply@phonepe.com>', 'Payment successful',
            f'You paid ₹{rupees(amount)} to {payee} via UPI. UPI transaction ID 6132{index}88. '
            'Get 2% cashback up to ₹25 on your next bill payment.',
            day + timedelta(days=index * 6), {'kind': 'purchase', 'amount_minor': amount, 'currency': 'INR', 'merchant': payee})
    # Refunds.
    for index, (name, address, merchant) in enumerate(SHOPS[:3] * 2):
        amount = 49900 + index * 10000
        add(f'refund-{index}', f'{name} <{address}>', 'Refund initiated',
            f'We have initiated a refund of ₹{rupees(amount)} for your returned item. It will reach your original '
            'payment method in 5-7 days.',
            day + timedelta(days=9 + index * 7), {'kind': 'refund', 'amount_minor': amount, 'currency': 'INR', 'merchant': merchant})
    # Bills that are due (not yet paid).
    for index, (name, address, merchant) in enumerate([('Airtel', 'ebill@airtel.in', 'Airtel'), ('Jio', 'bill@jio.com', 'Jio'),
                                                       ('Tata Power', 'billing@tatapower.com', 'Tata Power'),
                                                       ('ACT Fibernet', 'billing@actcorp.in', 'ACT Fibernet')] * 2):
        amount = 59900 + index * 15000
        add(f'bill-{index}', f'{name} <{address}>', f'Your {name} bill is ready',
            f'Your bill amount is ₹{rupees(amount)}. Due date: 5 {"Oct" if index % 2 else "Nov"}. '
            'Pay on time to avoid a late fee of ₹100.',
            day + timedelta(days=index * 11), {'kind': 'bill', 'amount_minor': amount, 'currency': 'INR', 'merchant': merchant})
    # Foreign currency invoices.
    for index, (name, address, merchant, amount) in enumerate([
            ('Amazon Web Services', 'aws-billing@amazon.com', 'Amazon Web Services', 1234),
            ('GitHub', 'billing@github.com', 'GitHub', 400), ('Notion', 'billing@makenotion.com', 'Notion', 1000),
            ('DigitalOcean', 'billing@digitalocean.com', 'DigitalOcean', 600)]):
        add(f'usd-{index}', f'{name} <{address}>', 'Your receipt',
            f'Receipt for your payment. Amount paid: ${amount // 100}.{amount % 100:02d} USD. Thank you.',
            day + timedelta(days=4 + index * 8), {'kind': 'purchase', 'amount_minor': amount, 'currency': 'USD', 'merchant': merchant})
    # Subscriptions as dated series: monthly, quarterly and yearly charges.
    plans = [('Netflix', 'info@account.netflix.com', 'Netflix', 64900, 'monthly', 30, 3),
             ('Spotify', 'no-reply@spotify.com', 'Spotify', 11900, 'monthly', 31, 3),
             ('YouTube', 'noreply-purchases@youtube.com', 'YouTube Premium', 12900, 'monthly', 30, 3),
             ('Google One', 'payments-noreply@google.com', 'Google One', 13000, 'monthly', 30, 2),
             ('Disney+ Hotstar', 'billing@hotstar.com', 'Disney+ Hotstar', 29900, 'quarterly', 91, 2),
             ('Amazon Prime', 'prime@amazon.in', 'Amazon Prime', 149900, 'yearly', 365, 2)]
    for name, address, merchant, amount, period, step, count in plans:
        for charge in range(count):
            received = date(2025, 10, 5) + timedelta(days=step * charge) if period == 'yearly' else \
                date(2026, 6, 5) + timedelta(days=step * charge)
            renew = received + timedelta(days=step)
            add(f'sub-{merchant.lower().replace(" ", "-").replace("+", "")}-{charge}', f'{name} <{address}>',
                f'Your {merchant} membership',
                f'Thanks for being a member. We charged ₹{rupees(amount)} for your {period} plan. '
                f'Your membership renews on {renew.strftime("%d %b %Y")}.',
                received, {'kind': 'subscription', 'amount_minor': amount, 'currency': 'INR', 'merchant': merchant},
                series={'merchant': merchant, 'recurring': True})
    # Irregular repeat purchases from one shop: not a subscription.
    for charge, gap in enumerate((0, 9, 47, 50)):
        amount = 39900 + charge * 7000
        add(f'repeat-amazon-{charge}', 'Amazon.in <auto-confirm@amazon.in>', 'Your Amazon.in order',
            f'Your order has shipped. Order Total: Rs. {rupees(amount)}', date(2026, 6, 1) + timedelta(days=gap),
            {'kind': 'purchase', 'amount_minor': amount, 'currency': 'INR', 'merchant': 'Amazon'},
            series={'merchant': 'Amazon', 'recurring': False})
    # Not transactions: adverts, price alerts, offers and newsletters with prices.
    adverts = [
        ('Apple', 'news@insideapple.apple.com', 'iPhone, now starting at ₹69,900', 'Meet the new iPhone, starting at ₹69,900. Shop now and get up to ₹5,000 off with select cards.'),
        ('Flipkart', 'offers@flipkart.com', 'Big Billion Days', 'Up to 80% off on electronics. Laptops starting at ₹24,990. Sale ends Sunday. Shop now.'),
        ('Amazon.in', 'price-alert@amazon.in', 'Price drop on your wishlist', 'Good news: the item on your wishlist dropped to ₹1,999 from ₹2,499. Deal of the day.'),
        ('Swiggy', 'offers@swiggy.in', 'Flat ₹150 off tonight', 'Use code TREAT150 for flat ₹150 off on orders above ₹499. Only for you, tonight.'),
        ('Zerodha', 'newsletter@zerodha.com', 'Market wrap', 'Nifty closed 1.2% higher. Reliance rose ₹42 to ₹2,960. This is not investment advice.'),
        ('MakeMyTrip', 'deals@makemytrip.com', 'Goa flights from ₹3,499', 'Fly to Goa from ₹3,499. Use code FLYGOA for ₹500 off. Coupon valid this week.'),
        ('Cult.fit', 'hello@cult.fit', 'Your membership offer', 'Get cultpass ELITE at ₹1,299 per month, 30% off for 3 days. Use code FIT30.'),
        ('Paytm', 'offers@paytm.com', 'Cashback offer', 'Get up to ₹100 cashback on your next 3 recharges of ₹199 or more. Use code SAVE100.'),
        ('Nykaa', 'offers@nykaa.com', 'Pink Friday Sale', 'Up to 50% off on top brands. Lipsticks starting at ₹199. Shop now, sale ends today.'),
        ('Croma', 'deals@croma.com', 'TVs from ₹12,990', 'Smart TVs starting at ₹12,990 with no-cost EMI from ₹1,083 per month. Shop now.'),
    ]
    for index, (name, address, subject, body) in enumerate(adverts * 2):
        add(f'advert-{index}', f'{name} <{address}>', subject, body, day + timedelta(days=index * 2), {'kind': 'none'})
    for row in rows:
        row['split'] = 'dev'
    # Held out: written after the rules were frozen, in other phrasings, and
    # never used to change them. This is the number that estimates real use.
    holdout = [
        ('BigBasket <care@bigbasket.com>', 'Order delivered', 'Your order is delivered.\nSubtotal ₹1,142.00\nSlot charges ₹30.00\nCoupon BB50 -₹50.00\nTotal Payable ₹1,122.00', {'kind': 'purchase', 'amount_minor': 112200, 'currency': 'INR', 'merchant': 'BigBasket'}),
        ('Uber Receipts <noreply@uber.com>', 'Your Tuesday evening trip with Uber', 'Thanks for riding. Trip fare ₹212.48, booking fee ₹18.00. Total ₹230.48 charged to UPI.', {'kind': 'purchase', 'amount_minor': 23048, 'currency': 'INR', 'merchant': 'Uber'}),
        ('Rapido <support@rapido.bike>', 'Ride invoice', 'Ride completed. Amount charged: INR 86. Rate your captain.', {'kind': 'purchase', 'amount_minor': 8600, 'currency': 'INR', 'merchant': 'Rapido'}),
        ('IRCTC <ticketadmin@irctc.co.in>', 'Booking confirmation', 'Ticket booked for PNR 4521. Ticket fare Rs. 1,045.00, convenience fee Rs. 17.70. Total amount Rs. 1,062.70.', {'kind': 'purchase', 'amount_minor': 106270, 'currency': 'INR', 'merchant': 'IRCTC'}),
        ('Axis Bank <alerts@axisbank.com>', 'Debit alert', 'INR 1,850.00 spent on Axis Bank Credit Card XX9012 at DMART on 12-08-26. Avl Limit: INR 1,48,150.00', {'kind': 'debit', 'amount_minor': 185000, 'currency': 'INR', 'merchant': 'DMART'}),
        ('Kotak Bank <alerts@kotak.com>', 'Account update', 'Rs 3,000.00 is debited from your a/c XX441 towards UPI/ola. Bal: Rs 22,390.10', {'kind': 'debit', 'amount_minor': 300000, 'currency': 'INR', 'merchant': 'Ola'}),
        ('Apple <no_reply@email.apple.com>', 'Your receipt from Apple', 'Billed to card ending 7781.\niCloud+ with 50 GB (Monthly) ₹75.00\nTOTAL ₹75.00\nRenews 14 Sep 2026.', {'kind': 'subscription', 'amount_minor': 7500, 'currency': 'INR', 'merchant': 'Apple'}),
        ('LinkedIn <billing@linkedin.com>', 'Your Premium receipt', 'Thank you for your payment of ₹1,567.00 for LinkedIn Premium Career. Your subscription renews automatically on 3 Oct 2026.', {'kind': 'subscription', 'amount_minor': 156700, 'currency': 'INR', 'merchant': 'LinkedIn'}),
        ('Zepto <noreply@zeptonow.com>', 'Order summary', 'Items: Milk, Bread, Eggs. Item total ₹212. Handling charge ₹9. Grand total ₹221. You saved ₹38!', {'kind': 'purchase', 'amount_minor': 22100, 'currency': 'INR', 'merchant': 'Zepto'}),
        ('Decathlon <orders@decathlon.in>', 'Invoice for your order', 'Invoice no. DEC-2209. Net amount payable: Rs 2,499 (incl. GST Rs 381). Paid via card.', {'kind': 'purchase', 'amount_minor': 249900, 'currency': 'INR', 'merchant': 'Decathlon'}),
        ('Myntra <updates@myntra.com>', 'Refund processed', 'Your refund of Rs. 1,099 has been processed to your bank account. It may take 3-5 days to reflect.', {'kind': 'refund', 'amount_minor': 109900, 'currency': 'INR', 'merchant': 'Myntra'}),
        ('BESCOM <billing@bescom.co.in>', 'Electricity bill', 'Bill for August generated. Amount due: Rs 1,284. Please pay before 20 Sep to avoid disconnection.', {'kind': 'bill', 'amount_minor': 128400, 'currency': 'INR', 'merchant': 'BESCOM'}),
        ('Vercel <billing@vercel.com>', 'Receipt #3021', 'Pro plan, 1 seat. Amount paid $20.00. Next invoice on Oct 12, 2026.', {'kind': 'subscription', 'amount_minor': 2000, 'currency': 'USD', 'merchant': 'Vercel'}),
        ('Udemy <no-reply@udemy.com>', 'Thanks for your purchase', 'Course: Python for Data Science. Original price ₹3,499. Paid ₹449.', {'kind': 'purchase', 'amount_minor': 44900, 'currency': 'INR', 'merchant': 'Udemy'}),
        ('Spotify <no-reply@spotify.com>', 'Premium Individual', 'Your card was charged ₹119 for Spotify Premium. Your plan renews on 9 Oct 2026.', {'kind': 'subscription', 'amount_minor': 11900, 'currency': 'INR', 'merchant': 'Spotify'}),
        ('Swiggy Instamart <noreply@swiggy.in>', 'Order delivered', 'Bill details: Item total ₹640, Delivery partner fee ₹25, Handling fee ₹6, Discount -₹80. To pay: ₹591.', {'kind': 'purchase', 'amount_minor': 59100, 'currency': 'INR', 'merchant': 'Swiggy Instamart'}),
        ('HDFC Bank <alerts@hdfcbank.net>', 'Refund credited', 'Rs. 799.00 has been credited back to your card ending 3321 as a refund from AMAZON.', {'kind': 'refund', 'amount_minor': 79900, 'currency': 'INR', 'merchant': 'Amazon'}),
        ('Jio <care@jio.com>', 'Recharge successful', 'Recharge of ₹299 done for 28 days. Plan valid till 30 Sep.', {'kind': 'purchase', 'amount_minor': 29900, 'currency': 'INR', 'merchant': 'Jio'}),
        ('Amazon.in <store-news@amazon.in>', 'Deals picked for you', 'Echo Dot now at ₹2,999 (was ₹5,499). Limited time deal. Shop now.', {'kind': 'none'}),
        ('PolicyBazaar <info@policybazaar.com>', 'Save on health insurance', 'Plans starting at ₹500 per month. Compare and save up to 25%. Get a free quote.', {'kind': 'none'}),
        ('Groww <noreply@groww.in>', 'Weekly market update', 'Your portfolio is up 3.1% this week. Top gainer: TCS (+₹84). Explore SIPs from ₹100.', {'kind': 'none'}),
        ('Ola <offers@olacabs.com>', 'Ride for less', 'Get 20% off on your next 3 rides, up to ₹75 per ride. Use code RIDE20.', {'kind': 'none'}),
        ('Swiggy <offers@swiggy.in>', 'Your favourites are back', 'Biryani starting at ₹149. Free delivery on orders above ₹199. Order now.', {'kind': 'none'}),
        ('Friend <fixture.rohan.mehta@gmail.com>', 'Dinner split', 'Hey, dinner came to Rs 2,400 so your share is Rs 600. Send it whenever.', {'kind': 'none'}),
    ]
    for index, (sender, subject, body, gold) in enumerate(holdout):
        add(f'holdout-{index}', sender, subject, body, date(2026, 8, 1) + timedelta(days=index), gold)
        rows[-1]['split'] = 'holdout'
    return rows


def main():
    rows = cases()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'emails.json').write_text(json.dumps(rows, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
    kinds = {}
    for row in rows:
        kinds[row['gold']['kind']] = kinds.get(row['gold']['kind'], 0) + 1
    (OUT / 'provenance.json').write_text(json.dumps({
        'name': 'MailMind synthetic receipts benchmark', 'license': 'CC0-1.0',
        'generator': 'scripts/build_receipts_benchmark.py', 'cases': len(rows), 'kinds': kinds,
        'notes': 'Synthetic receipts and adverts written for this project; merchant names appear as senders only.',
    }, indent=1) + '\n', encoding='utf-8')
    (OUT / 'LICENSE.txt').write_text(
        'The synthetic receipts benchmark in this folder is dedicated to the public domain under CC0 1.0:\n'
        'https://creativecommons.org/publicdomain/zero/1.0/\n', encoding='utf-8')
    print(f'Wrote {len(rows)} cases: {kinds}')


if __name__ == '__main__':
    main()
