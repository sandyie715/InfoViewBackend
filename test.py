import smtplib

server = smtplib.SMTP('smtp.gmail.com', 587)
server.starttls()
server.login('sanjay715@infomaticscorp.com', 'pwdf qxmt zvwc afsq')
print("✅ Connection successful!")
# MAIL_USERNAME=sanjay715@infomaticscorp.com
# MAIL_PASSWORD=pwdf qxmt zvwc afsq