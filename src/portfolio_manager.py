import pandas as pd
import datetime
import pytz
try:
    import MetaTrader5 as mt5
except ImportError:
    from . import mock_metatrader5 as mt5

class PortfolioManager:
    """
    Manages the trading portfolio, including P&L tracking, position management,
    and automatic trade closure based on predefined rules.
    """
    def __init__(self, mt5_connection, trade_executor, config):
        self.mt5 = mt5_connection
        self.trade_executor = trade_executor
        self.config = config

        # Portfolio state
        self.portfolio_history = []
        self.realized_pnl = 0.0
        self.unrealized_pnl = 0.0

        # Position tracking with enhanced details
        self.open_positions = {} # Key: ticket, Value: position details dict

        # Position closure rules from config
        self.profit_target = float(self.config['trading'].get('profit_target', '75.0'))
        self.stop_loss = float(self.config['trading'].get('stop_loss', '-50.0'))
        self.max_position_duration_hours = int(self.config['trading'].get('max_position_duration_hours', '4'))
        self.enable_trailing_stop = self.config['trading'].getboolean('enable_trailing_stop', True)
        self.trailing_stop_trigger_pnl = float(self.config['trading'].get('trailing_stop_trigger_pnl', '30.0'))
        self.trailing_stop_distance_pnl = float(self.config['trading'].get('trailing_stop_distance_pnl', '15.0'))

    def get_account_info(self):
        """Gets the latest account information from MT5."""
        return self.mt5.account_info()

    def get_open_positions(self):
        """
        Fetches all open positions from MT5 and returns them as a DataFrame.
        """
        positions = self.mt5.positions_get()
        if not positions:
            return pd.DataFrame()

        return pd.DataFrame(list(positions), columns=positions[0]._asdict().keys())

    def update_portfolio_state(self):
        """
        The core method to update the entire portfolio state. It fetches current positions,
        updates P&L, and checks against closure rules.
        """
        positions_df = self.get_open_positions()
        now_utc = datetime.datetime.now(pytz.UTC)

        # Reset unrealized P&L for recalculation
        self.unrealized_pnl = 0.0

        current_tickets = set()

        if not positions_df.empty:
            for _, pos in positions_df.iterrows():
                ticket = pos['ticket']
                current_tickets.add(ticket)

                # Update unrealized P&L
                self.unrealized_pnl += pos['profit']

                # Update or add position to our internal tracking
                if ticket not in self.open_positions:
                    self.open_positions[ticket] = {
                        'ticket': ticket,
                        'symbol': pos['symbol'],
                        'open_time': pd.to_datetime(pos['time'], unit='s', utc=True),
                        'direction': 'BUY' if pos['type'] == 0 else 'SELL',
                        'volume': pos['volume'],
                        'open_price': pos['price_open'],
                        'peak_pnl': pos['profit'] # Initialize peak P&L
                    }
                else:
                    # Update peak P&L for trailing stop logic
                    self.open_positions[ticket]['peak_pnl'] = max(
                        self.open_positions[ticket].get('peak_pnl', pos['profit']),
                        pos['profit']
                    )

                # Check position against closure rules
                self._check_position_for_closure(pos, now_utc)

        # Clean up closed positions from our internal tracker
        closed_tickets = set(self.open_positions.keys()) - current_tickets
        for ticket in closed_tickets:
            # Here you could add logic to calculate realized P&L if needed,
            # though MT5's history is the source of truth for that.
            del self.open_positions[ticket]

        # Log the current state of the portfolio
        self._log_portfolio_history(now_utc)

    def _check_position_for_closure(self, position, current_time_utc):
        """
        Checks a single position against all automatic closure rules.
        """
        ticket = position['ticket']
        pnl = position['profit']

        # 1. Profit Target
        if pnl >= self.profit_target:
            print(f"CLOSURE RULE: Profit target hit for ticket {ticket} ({pnl:.2f} >= {self.profit_target:.2f})")
            self.trade_executor.close_trade(ticket, comment="Profit Target")
            return

        # 2. Stop Loss
        if pnl <= self.stop_loss:
            print(f"CLOSURE RULE: Stop loss hit for ticket {ticket} ({pnl:.2f} <= {self.stop_loss:.2f})")
            self.trade_executor.close_trade(ticket, comment="Stop Loss")
            return

        # 3. Time-based Exit
        open_time = self.open_positions[ticket]['open_time']
        duration = current_time_utc - open_time
        if duration.total_seconds() / 3600 >= self.max_position_duration_hours:
            print(f"CLOSURE RULE: Max duration exceeded for ticket {ticket} ({duration})")
            self.trade_executor.close_trade(ticket, comment="Max Duration")
            return

        # 4. Trailing Stop
        if self.enable_trailing_stop:
            peak_pnl = self.open_positions[ticket].get('peak_pnl', 0)
            if peak_pnl >= self.trailing_stop_trigger_pnl:
                # Trailing stop is active, check if it has been breached
                trailing_stop_level = peak_pnl - self.trailing_stop_distance_pnl
                if pnl <= trailing_stop_level:
                    print(f"CLOSURE RULE: Trailing stop hit for ticket {ticket} (Peak PnL: {peak_pnl:.2f}, Current PnL: {pnl:.2f}, Stop Level: {trailing_stop_level:.2f})")
                    self.trade_executor.close_trade(ticket, comment="Trailing Stop")
                    return

    def _log_portfolio_history(self, timestamp):
        """
        Logs the current portfolio state to the history tracker.
        """
        account_info = self.get_account_info()
        if not account_info:
            return

        current_equity = account_info.equity
        current_balance = account_info.balance

        # Update realized P&L based on balance changes (simplified)
        # A more robust approach would use mt5.history_deals_get
        if self.portfolio_history:
            self.realized_pnl = current_balance - self.portfolio_history[0]['balance']
        else:
            # Store initial balance
            self.portfolio_history.append({
                'timestamp': timestamp,
                'equity': current_equity,
                'balance': current_balance,
                'unrealized_pnl': self.unrealized_pnl,
                'realized_pnl': 0,
                'open_positions': len(self.open_positions)
            })
            return

        self.portfolio_history.append({
            'timestamp': timestamp,
            'equity': current_equity,
            'balance': current_balance,
            'unrealized_pnl': self.unrealized_pnl,
            'realized_pnl': self.realized_pnl,
            'open_positions': len(self.open_positions)
        })

    def get_portfolio_summary(self):
        """
        Returns a summary of the current portfolio state.
        """
        account_info = self.get_account_info()
        if not account_info:
            return "Could not retrieve account info."

        summary = (
            f"**Portfolio Summary**\n"
            f"- Equity: {account_info.equity:.2f}\n"
            f"- Balance: {account_info.balance:.2f}\n"
            f"- Unrealized P&L: {self.unrealized_pnl:.2f}\n"
            f"- Realized P&L: {self.realized_pnl:.2f}\n"
            f"- Open Positions: {len(self.open_positions)}"
        )
        return summary

    def get_trade_history(self, start_date=None, end_date=None):
        """
        Gets the trading history for a given period from MT5.
        This is a simplified implementation focusing on closed deals.
        """
        if start_date is None:
            # Default to the start of the current day in UTC
            start_date = datetime.datetime.now(pytz.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        if end_date is None:
            end_date = datetime.datetime.now(pytz.utc)

        # Fetch deals from MT5 history
        deals = self.mt5.history_deals_get(start_date, end_date)
        if deals is None or not deals:
            print("No trading history found for the specified period.")
            return []

        deals_df = pd.DataFrame(list(deals), columns=deals[0]._asdict().keys())

        # Filter for deals that represent closing a position (entry type = 1)
        closed_deals = deals_df[deals_df['entry'] == 1].copy()

        if closed_deals.empty:
            return []

        # Convert to a list of dicts for easier use
        history_list = []
        for _, deal in closed_deals.iterrows():
            history_list.append({
                'ticket': deal['position_id'],
                'symbol': deal['symbol'],
                'pnl': deal['profit'],
                'comment': deal['comment'],
                'close_time': pd.to_datetime(deal['time'], unit='s', utc=True)
            })

        return history_list
