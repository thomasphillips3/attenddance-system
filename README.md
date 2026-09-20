# AttendDANCE - RFID Attendance System

A complete RFID-based attendance system for dance studios running on Raspberry Pi.

## Features

- 🎯 **RFID Check-in**: Students tap RFID cards to check in before classes
- 👨‍🏫 **Teacher Dashboard**: Web interface for viewing attendance and managing students
- 📱 **Responsive Design**: Mobile-first design that works on all devices
- 🔄 **Progressive Web App**: Installable PWA with offline capabilities
- 🗄️ **Local Storage**: SQLite database for reliable local data storage
- 🔐 **Authentication**: Secure teacher login system
- 🎨 **Modern UI**: Clean, intuitive interface built with Tailwind CSS

### Billing funds

Every charge and payment belongs to one of two funds: **Studio** (tuition,
costumes, shoes, registration, other) or **Company / Foundation** (competition,
convention, transportation, lodging, observer, costume rental, company dues,
company tickets). Company money is owed to the LSODance Foundation, so the two
are never netted: a Company payment only ever settles Company charges and every
balance surface (billing table, ledgers, parent portal, reminders, late fees,
statements, CSV exports) shows the two funds separately. The category-to-fund
map lives in `app/funds.py` and is the only place it is defined.

Foundation payment destinations (Zelle, Cash App, Square link) are set in the
"Company / Foundation payments" block on `/settings`; parents see them when they
pay a Company balance. If a parent pays the wrong account, an admin can use
"Move money between funds" on the student's ledger, which posts two offsetting
`transfer` rows sharing a reference and writes an audit entry.

## Hardware Requirements

- **Raspberry Pi 4 or 5** (recommended) running Raspberry Pi OS Bookworm
- **MFRC522 RFID module** connected via SPI
- **RFID cards/tags** for students  
- **MicroSD card** (32GB+ recommended)
- **Power supply** (official Raspberry Pi power supply recommended)
- Optional: Touchscreen display for kiosk mode

### Raspberry Pi Compatibility

| Pi Model | OS Support | Performance | Notes |
|----------|------------|-------------|-------|
| **Pi 5** | ✅ Bookworm | Excellent | Latest hardware, best performance |
| **Pi 4** | ✅ Bookworm/Bullseye/Buster | Very Good | Recommended for production |
| **Pi 3B+** | ✅ Bookworm/Bullseye/Buster | Good | Works well for small installations |
| **Pi 3B** | ⚠️ Bullseye/Buster only | Fair | Minimum recommended |
| **Pi Zero 2W** | ⚠️ Limited | Fair | Single user only |

## Quick Start

### 1. Hardware Setup
Connect your MFRC522 RFID module to the Raspberry Pi:

```
MFRC522 Pin    →  Raspberry Pi Pin
SDA (NSS)      →  Pin 24 (GPIO 8, CE0)
SCK            →  Pin 23 (GPIO 11, SCLK) 
MOSI           →  Pin 19 (GPIO 10, MOSI)
MISO           →  Pin 21 (GPIO 9, MISO)
IRQ            →  Not connected
GND            →  Pin 6 (Ground)
RST            →  Pin 22 (GPIO 25)
3.3V           →  Pin 1 (3.3V)
```

### 2. Software Installation

#### For Raspberry Pi 4/5 with Bookworm OS (Recommended)

```bash
# Clone the repository
git clone https://github.com/thomasphillips3/attenddance-rfid-system.git
cd attenddance-rfid-system

# Create virtual environment (strongly recommended)
python3 -m venv venv
source venv/bin/activate

# Update pip and install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# Enable SPI interface
sudo raspi-config
# Navigate to: Interface Options → SPI → Enable → Reboot
```

#### For Older Pi Models (Pi 3B/3B+) or Bullseye OS

If you encounter issues with the latest versions, use the legacy requirements:

```bash
# Create a legacy requirements file
cat > requirements-legacy.txt << 'EOF'
# Legacy versions for older Pi models
Flask==2.3.3
Flask-SQLAlchemy==3.0.5
Flask-Login==0.6.3
Flask-WTF==1.1.1
WTForms==3.0.1
Werkzeug==2.3.7
mfrc522==0.0.7
RPi.GPIO==0.7.1
spidev==3.6
bcrypt==4.0.1
python-dotenv==1.0.0
PyJWT==2.8.0
requests==2.31.0
python-dateutil==2.8.2
gunicorn==21.2.0
EOF

# Install legacy versions
pip install -r requirements-legacy.txt
```

#### For Raspbian Buster (RPi 4/3B+)

If you're running **Raspbian Buster**, use the Buster-specific requirements:

```bash
# Check your OS version
cat /etc/os-release

# Install Buster-compatible versions
pip install -r requirements-buster.txt

# Alternative: Install directly from pre-built wheels
pip install --index-url https://www.piwheels.org/simple/ -r requirements-buster.txt
```

**Buster-Specific Setup:**
```bash
# Update system packages first
sudo apt update && sudo apt upgrade -y

# Install required system dependencies
sudo apt install -y python3-dev python3-pip python3-venv
sudo apt install -y build-essential libffi-dev libssl-dev
sudo apt install -y git

# Check Python version (should be 3.7 or 3.8)
python3 --version

# Enable SPI and GPIO
sudo raspi-config
# Navigate to: Interface Options → SPI → Enable → Finish → Reboot
```

#### Installation Troubleshooting

**If pip installation fails:**
```bash
# Install system dependencies first
sudo apt update
sudo apt install -y python3-dev python3-pip python3-venv
sudo apt install -y build-essential libffi-dev libssl-dev

# For Pi 5 with new GPIO handling
sudo apt install -y python3-lgpio

# Retry installation
pip install -r requirements.txt
```

**For compilation errors on older Pi models:**
```bash
# Install pre-compiled wheels from piwheels
pip install --index-url https://www.piwheels.org/simple/ -r requirements.txt
```

### 3. Running the Application

#### Development Mode
```bash
# Ensure virtual environment is activated
source venv/bin/activate

# Run the application
python run.py
```

Access the web interface at `http://localhost:5000` or `http://your-pi-ip:5000`

**Default Login:**
- Username: `admin`
- Password: `admin123`

*⚠️ **Important**: Change the default password immediately after first login!*

#### Production Mode

**Method 1: Using Gunicorn**
```bash
# Set production environment
export FLASK_ENV=production
export SECRET_KEY=your-secret-key-here

# Run with Gunicorn (recommended for production)
gunicorn -w 2 -b 0.0.0.0:5000 --timeout 120 run:app
```

**Method 2: Background Process**
```bash
# Run as background service
nohup python run.py > attendance.log 2>&1 &
```

#### System Service Setup (Production)

Create a systemd service for automatic startup:

```bash
# Create service file
sudo tee /etc/systemd/system/attenddance.service << 'EOF'
[Unit]
Description=AttenDANCE RFID Attendance System
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/attenddance-rfid-system
Environment=FLASK_ENV=production
Environment=SECRET_KEY=change-this-secret-key
ExecStart=/home/pi/attenddance-rfid-system/venv/bin/python run.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# Enable and start the service
sudo systemctl daemon-reload
sudo systemctl enable attenddance
sudo systemctl start attenddance
```

## Project Structure

```
attenddance-rfid-system/
├── app/                    # Flask application
│   ├── __init__.py        # App factory and initialization
│   ├── models.py          # Database models
│   ├── auth/              # Authentication routes
│   ├── api/               # REST API routes
│   ├── main/              # Main application routes
│   ├── templates/         # HTML templates
│   └── static/            # CSS, JS, images, PWA files
├── rfid/                  # RFID module code
│   ├── __init__.py
│   ├── reader.py          # RFID reading logic
│   └── service.py         # Background RFID service
├── config/                # Configuration files
│   └── config.py          # Application configuration
├── data/                  # Database files (auto-created)
├── venv/                  # Virtual environment (after setup)
├── requirements.txt       # Python dependencies (latest)
├── requirements-legacy.txt # Legacy versions for older Pi
├── requirements-buster.txt # Buster OS specific versions
├── run.py                # Application entry point
└── README.md
```

## Configuration

The application automatically:
- Creates the SQLite database (`data/attendance.db`)
- Sets up database tables
- Creates a default admin user
- Creates necessary directories

### Environment Variables
Customize the application using environment variables:

```bash
# Database
export DATABASE_URL=sqlite:///data/attendance.db

# Security (IMPORTANT: Change these!)
export SECRET_KEY=your-very-secure-secret-key-here
export JWT_SECRET_KEY=your-jwt-secret-key-here

# RFID Settings
export RFID_ENABLED=true
export RFID_SPI_DEV=0
export RFID_RST_PIN=25

# Server Settings
export FLASK_HOST=0.0.0.0
export FLASK_PORT=5000
export FLASK_ENV=production
```

### Performance Optimization for Raspberry Pi

**For Pi 4/5:**
```bash
# In config/config.py or environment variables
export STUDENTS_PER_PAGE=100
export ATTENDANCE_PER_PAGE=200
```

**For Pi 3B/3B+:**
```bash
# Reduced settings for older hardware
export STUDENTS_PER_PAGE=50
export ATTENDANCE_PER_PAGE=100
```

## API Endpoints

### Authentication
- `POST /api/auth/login` - Teacher login
- `POST /api/auth/logout` - Logout
- `GET /api/auth/me` - Get current user info

### Students
- `GET /api/students` - List all students
- `POST /api/students` - Add new student
- `PUT /api/students/<id>` - Update student
- `DELETE /api/students/<id>` - Remove student
- `POST /api/students/<id>/assign-rfid` - Assign RFID card

### Attendance
- `GET /api/attendance` - Get attendance records
- `POST /api/attendance/checkin` - Manual check-in
- `GET /api/attendance/today` - Today's attendance

### Billing
- `GET /api/balances` - Per-student balances, one block per fund
- `GET /api/students/<id>/ledger` - Per-fund ledger with a running balance per fund
- `POST /api/students/<id>/fund-transfer` - Admin: move money between funds (two offsetting rows + audit)
- `GET /api/payment-options` - Parent-facing payment destinations per fund
- `GET /api/reports/transactions.csv?fund=` - Ledger export with a Fund column and per-fund subtotals

### Classes
- `GET /api/classes` - List classes
- `POST /api/classes` - Create class
- `PUT /api/classes/<id>` - Update class

## Troubleshooting

### RFID Not Working
1. **Check SPI is enabled:** `sudo raspi-config` → Interface Options → SPI → Enable
2. **Verify wiring connections** match the hardware setup above
3. **Check permissions:** Ensure your user is in the required groups:
   ```bash
   sudo usermod -a -G spi,gpio,i2c $USER
   sudo reboot
   ```
4. **Test RFID module:** Check console output when starting the application
5. **For Pi 5:** Additional GPIO setup may be required:
   ```bash
   sudo apt install python3-lgpio
   ```

### Database Issues
- **Reset database:** Delete `data/attendance.db` and restart the application
- **Backup database:** `cp data/attendance.db data/attendance_backup.db`
- **Database permissions:** Ensure the application can write to the `data/` directory

### Service Issues
- **Check status:** `sudo systemctl status attenddance`
- **View logs:** `sudo journalctl -u attenddance -f`
- **Restart service:** `sudo systemctl restart attenddance`

### Installation Issues

**ImportError for RPi.GPIO:**
```bash
# For Pi 5 or newer Bookworm installations
sudo apt install python3-rpi.gpio

# Or use lgpio for Pi 5
sudo apt install python3-lgpio
```

**SPI Permission Errors:**
```bash
sudo usermod -a -G spi,gpio,i2c $USER
sudo chmod 666 /dev/spidev0.0
sudo reboot
```

**Memory Issues on Older Pi Models:**
```bash
# Increase swap space
sudo dphys-swapfile swapoff
sudo nano /etc/dphys-swapfile
# Change CONF_SWAPSIZE=100 to CONF_SWAPSIZE=1024
sudo dphys-swapfile setup
sudo dphys-swapfile swapon
```

**Buster-Specific Issues:**
```bash
# If you get SSL/TLS errors during pip install
pip install --trusted-host pypi.org --trusted-host pypi.python.org --trusted-host files.pythonhosted.org -r requirements-buster.txt

# If pip is outdated on Buster
python3 -m pip install --upgrade pip setuptools wheel

# Check and fix locale issues
sudo dpkg-reconfigure locales
export LC_ALL=C.UTF-8
export LANG=C.UTF-8

# For GPIO permission issues on Buster
sudo usermod -a -G gpio,spi,i2c $USER
sudo chmod 666 /dev/gpiomem
sudo reboot
```

### Network Access Issues

**Firewall (if enabled):**
```bash
sudo ufw allow 5000
```

**Access from other devices:**
```bash
# Find your Pi's IP address
hostname -I

# Access from other devices: http://PI_IP_ADDRESS:5000
```

## Development

### Running in Development Mode
```bash
export FLASK_ENV=development
export FLASK_DEBUG=true
python run.py
```

### Testing Without RFID Hardware
The application gracefully handles missing RFID hardware and will run without it for development purposes. Manual check-in options are available in the web interface.

### Adding New Features
The modular structure makes it easy to extend:
- Add new routes in `app/main/routes.py` or `app/api/routes.py`
- Create new database models in `app/models.py`
- Add templates in `app/templates/`
- Extend RFID functionality in `rfid/`

## Security Considerations

🔒 **Important Security Notes:**

1. **Change default passwords** immediately
2. **Use strong SECRET_KEY** in production
3. **Enable HTTPS** for production deployments
4. **Restrict network access** to trusted devices
5. **Regular backups** of the database
6. **Keep system updated**: `sudo apt update && sudo apt upgrade`

## License

MIT License - See LICENSE file for details.

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Make your changes
4. Test on a Raspberry Pi
5. Commit your changes (`git commit -m 'Add amazing feature'`)
6. Push to the branch (`git push origin feature/amazing-feature`)
7. Open a Pull Request

## Support

For issues and questions:
- **GitHub Issues**: https://github.com/thomasphillips3/attenddance-rfid-system/issues
- **Documentation**: Check this README and code comments
- **Hardware Issues**: Verify connections and Raspberry Pi configuration
- **Pi 5 Specific**: Check for latest GPIO library updates

---

**Built with ❤️ for dance studios using Raspberry Pi** 
