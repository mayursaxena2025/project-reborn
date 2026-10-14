# Sassbyte Consultancy Website

A free, open-source static website for an AI and software consultancy brand.

## Included pages

- Home
- Services
- Solutions
- About
- Contact

## Local preview

From the project folder:

```bash
cd /Users/ijprmasss/project/sassbyte-store
python3 -m http.server 8000
```

Then open:

- http://localhost:8000/

## OpenLead

OpenLead is the local-first lead acquisition desk linked from the site navigation. Run its app separately during development:

```bash
PORT=8010 python3 AI-agent-assistence/app.py
```

Open <http://127.0.0.1:8010> for the app. The deployed Nginx configuration proxies `/openlead/` to this service on `127.0.0.1:8010` without an authentication prompt. Anyone who can reach the app can view and change its leads, settings, and integration credentials; expose it publicly only if that is intended. Keep the service bound to loopback and run it under a process manager. The app's setup, integration contract, consent requirements, and MVP boundaries are documented on `openlead.html` and in `AI-agent-assistence/README.md`.

## EC2 deployment on Ubuntu (free open-source stack)

1. Connect to your EC2 instance.
2. Install Nginx and Git:

```bash
sudo apt update
sudo apt install -y nginx git
```

3. Copy the project files to the server:

```bash
cd /var/www
sudo mkdir -p sassbyte
sudo chown -R $USER:$USER sassbyte
cd sassbyte
```

Then upload or clone the project contents here.

4. Configure Nginx:

```bash
sudo nano /etc/nginx/sites-available/sassbyte
```

Use this configuration:

```nginx
server {
    listen 80;
    server_name sassbyte.store www.sassbyte.store;
    root /var/www/sassbyte;
    index index.html;

    location / {
        try_files $uri $uri/ =404;
    }
}
```

Enable it:

```bash
sudo ln -s /etc/nginx/sites-available/sassbyte /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx
```

5. Open port 80 in AWS Security Group if needed.

## Namecheap DNS settings

At Namecheap, set your DNS records to point to the EC2 public IP:

- A Record: `@` -> `YOUR_EC2_PUBLIC_IP`
- A Record: `www` -> `YOUR_EC2_PUBLIC_IP`

If you use Cloudflare, you can also use the EC2 IP there instead.

## Optional HTTPS with free certs

Install Certbot and get a free Let’s Encrypt certificate:

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d sassbyte.store -d www.sassbyte.store
```

## Notes

This project uses free, open-source tools only:

- HTML
- CSS
- JavaScript
- Nginx
- Ubuntu EC2
- Let’s Encrypt

It is ready for a consultancy brand site and can be extended with a backend later if needed.
