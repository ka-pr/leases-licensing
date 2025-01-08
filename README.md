# Leases Licensing System

The Leases and Licensing System is used by customers to submit a Registration of Interest or a Lease/Licence proposal in case they want to commercially use land managed by the Department and pay for annual fees for issued leases or licences. The system is used by Department staff to process the proposals and to manage issued leases and licences, manage the annual fees and manage the outcomes of competitive processes.

It is a database-backed Django application, using REST API with Vue.js as the client side app and integrates into the Ledger system.

# Requirements and installation

- Python (3.8.x)
- PostgreSQL (>=11)
- Node JS (>=16)

Python library requirements should be installed using Python Poetry:

To install Poetry, issue these commands:

`sudo apt install python3-venv`
`sudo pip install poetry`

# Development Environment

## Django app
In the root of the cloned repository, `poetry install` will create a .venv folder similar to virtualenv.

Start the Django app with `./run_dev.sh <port number>`.  Convenience scripts `collectstatic.sh` and `shell_plus.sh` are also available.

## Ledger
A Ledger server must be run prior to the Leases Licensing Django application.

The `LEDGER_API_URL` env var assumes that the server will be run with no port specified, i.e. using the default port 8000.

The db listed by the `LEDGER_DATABASE_URL` env var holds user, organisation and other corporate data.
Creating a new user or changing a user password must be done in the Ledger app/db.

## Vue JS
Root of the Vue Js folder has package.json, which has the list of packages to be installed plus commands on to build the software and start the dev server.

In the root folder, install packages with `npm install`.

Then, run `npm run build` to build the software and move the output files to `leaseslicensing/static/leaseslicensing_vue`.

The build files are made available to the Django app by running `./collectstatic.sh`.

Start the dev server with `npm run dev`.

# Environment variables

A `.env` file should be created in the project root and used to set
required environment variables at run time. Example content:

    DEBUG=True
    SECRET_KEY='thisismysecret'
    DATABASE_URL='postgis://user:pw@localhost:port/db_name'
    EMAIL_HOST='SMTP_HOST'
    BPOINT_USERNAME='BPOINT_USER'
    BPOINT_PASSWORD='BPOINT_PW
    BPOINT_BILLER_CODE='1234567'
    BPOINT_MERCHANT_NUM='BPOINT_MERCHANT_NUM'
    BPAY_BILLER_CODE='987654'
    DEFAULT_FROM_EMAIL='FROM_EMAIL_ADDRESS'
    NOTIFICATION_EMAIL='NOTIF_RECIPIENT_1, NOTIF_RECIPIENT_2'
    NON_PROD_EMAIL='NON_PROD_RECIPIENT_1, NON_PROD_RECIPIENT_2'
    EMAIL_INSTANCE='DEV'
    PRODUCTION_EMAIL=False
    BPAY_ALLOWED=False
    SITE_PREFIX='prefix'
    SITE_DOMAIN='SITE_DOMAIN'
    LEDGER_GST=10
    DISABLE_EMAIL=True
    DJANGO_HTTPS=True
    CRON_NOTIFICATION_EMAIL='email'
    ENABLE_DJANGO_LOGIN=True
    OSCAR_SHOP_NAME='shop_name'
    LEDGER_DATABASE_URL='postgis://user:pw@localhost:port/db_name'
    LEDGER_API_URL="http://localhost:8000"
    LEDGER_API_KEY="API_KEY"
    # Below is required to run Vue Js front end with hot reload
    # Below prints emails to screen instead of sending via mail server
    CONSOLE_EMAIL_BACKEND=True
