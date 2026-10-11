# astro-slew-ai
# K-Star Interactor 

An AI-powered, voice-controlled equatorial telescope mount interface. K-Star Interactor bridges the gap between natural language and the INDI protocol by leveraging Google Gemini and the NASA JPL Horizons API to dynamically resolve, track, and slew to celestial bodies without requiring any local coordinate databases.

## Features
* **Natural Language & Voice Control:** Speak or type your target (e.g., *"Center the Ring Nebula"*, *"Point at Jupiter"*). Built-in speech-to-text uses the Google Web Speech API.
* **Smart AI Target Resolution:** Uses **Google Gemini (Flash)** to classify targets on the fly. 
    * **Fixed Background Objects:** (Stars, Nebulae, Galaxies) Gemini resolves J2000 standard coordinates directly.
    * **Solar-System Bodies:** (Planets, Moons, Comets) Gemini determines the exact NASA JPL Horizons command ID.
* **Live NASA Ephemerides:** Connects to the **NASA JPL Horizons API** to fetch real-time, epoch-of-date (apparent geocentric/topocentric) RA/DEC for moving bodies.
* **INDI Protocol Integration:** Full PyQt6 graphical interface to connect to, monitor, and command INDI-compatible mounts (tested with LX200 Classic and Telescope Simulator).

## Architecture
1. **User Input:** Free-text or voice prompt is captured.
2. **Classification (Gemini):** Prompt is analyzed. If it's a fixed object, Gemini returns RA/DEC. If it's a solar-system body, Gemini returns the JPL Horizons ID.
3. **Telemetry (Horizons):** If a JPL ID is provided, the app queries the Horizons API for the current minute's apparent position.
4. **Execution (PyIndi):** Coordinates are converted and sent to the INDI server to initiate a mount slew.

## Prerequisites

* An active **INDI Server** (`indiserver`) running locally or on your network.
* A free **Google AI Studio API Key** (for Gemini).
* **Python 3.8+**

## Installation

1. **Clone the repository:**
   ```bash
   git clone [https://github.com/yourusername/k-star-interactor.git](https://github.com/somyama4753l-lang/k-star-interactor.git)
   cd k-star-interactor
