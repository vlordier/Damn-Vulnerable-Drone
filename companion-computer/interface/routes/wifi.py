
from flask import Blueprint, render_template
from flask_login import login_required

wifi_bp = Blueprint('wifi', __name__)

@wifi_bp.route('/wifi-network')
@login_required
def wifi_network():
    return render_template('wifiNetwork.html')