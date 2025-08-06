from .base_agent import Agent
from ..data_collector import EconomicCalendarCollector
import pandas as pd

class DataAnalystAgent(Agent):
    def __init__(self, name, mt5_collector):
        super().__init__(name)
        self.mt5_collector = mt5_collector
        self.economic_calendar_collector = EconomicCalendarCollector()

    def execute(self, task):
        """
        Executes a data collection task.
        """
        if task['source'] == 'mt5':
            # Ensure MT5 connection is established
            if not self.mt5_collector.connect():
                print(f"Failed to connect to MT5 for symbol {task.get('symbol', 'unknown')}")
                return None
            
            data = {}
            success_count = 0
            total_timeframes = len(task['timeframes'])
            
            for timeframe in task['timeframes']:
                try:
                    # Handle different formats of num_bars
                    if isinstance(task['num_bars'], dict):
                        num_bars = task['num_bars'].get(timeframe, 100)
                    else:
                        num_bars = task['num_bars']
                    
                    df = self.mt5_collector.get_historical_data(
                        task['symbol'], timeframe, num_bars
                    )
                    
                    if df is not None and not df.empty:
                        data[timeframe] = df
                        success_count += 1
                    else:
                        print(f"No data received for {task['symbol']} on timeframe {timeframe}")
                        data[timeframe] = None
                        
                except Exception as e:
                    print(f"Error fetching data for {task['symbol']} on timeframe {timeframe}: {e}")
                    data[timeframe] = None
            
            # Disconnect after data collection
            self.mt5_collector.disconnect()
            
            # Return data only if we got at least some successful results
            if success_count > 0:
                return data
            else:
                print(f"Failed to get any data for symbol {task['symbol']}")
                return None
                
        elif task['source'] == 'economic_calendar':
            return self.economic_calendar_collector.get_economic_calendar()
        return pd.DataFrame()
