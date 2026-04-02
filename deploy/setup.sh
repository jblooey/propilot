#!/bin/bash
# Run this on your DigitalOcean droplet as root

# 1. Update system + install deps
apt update && apt upgrade -y
apt install -y python3 python3-pip nginx certbot python3-certbot-nginx git

# 2. Clone repo
git clone https://github.com/jblooey/propilot.git /root/propilot

# 3. Install Python packages
pip3 install -r /root/propilot/requirements.txt

# 4. Copy service files
cp /root/propilot/deploy/propilot-app.service    /etc/systemd/system/
cp /root/propilot/deploy/propilot-runner.service /etc/systemd/system/

# 5. Enable and start services
systemctl daemon-reload
systemctl enable propilot-app propilot-runner
systemctl start propilot-app propilot-runner

# 6. Copy nginx config (edit YOUR_DOMAIN_HERE first)
cp /root/propilot/deploy/nginx.conf /etc/nginx/sites-available/propilot
ln -s /etc/nginx/sites-available/propilot /etc/nginx/sites-enabled/propilot
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl restart nginx

echo "Done. Next: scp your .env and data files, then run certbot."
