import time
import logging
import uuid

class AlertManager:
    def __init__(self):
        self.alerts = []

    def add_alert(self, message):
        uid = uuid.uuid4()
        self.alerts.append({'id': uid, 'message': message})
        logging.info(f'Alert added: {uid}')  # Updated f-string

    def show_alerts(self):
        for alert in self.alerts:
            print(f'ID: {alert['id']}, Message: {alert['message']}')  # Updated f-string

class DevPanel:
    def _show_dev_panel(self):
        uid = uuid.uuid4() # Define uid before using
        logging.info(f'Dev panel shown: {uid}')  # Updated f-string

class CodeProcessor:
    def _process_code(self, code):
        uid = uuid.uuid4() # Define uid before using
        # Processing code logic here
        logging.info(f'Code processed: {uid}')  # Updated f-string

    def _process_password(self, password):
        uid = uuid.uuid4() # Define uid before using
        # Processing password logic here
        logging.info(f'Password processed: {uid}')  # Updated f-string

if __name__ == '__main__':
    alert_manager = AlertManager()
    alert_manager.add_alert('Test Alert')
    alert_manager.show_alerts()

    dev_panel = DevPanel()
    dev_panel._show_dev_panel()

    code_processor = CodeProcessor()
    code_processor._process_code('print("Hello World")')
    code_processor._process_password('secure_password')
