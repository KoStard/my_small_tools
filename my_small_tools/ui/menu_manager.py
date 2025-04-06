#!/usr/bin/env python
from prompt_toolkit import prompt
from typing import Callable, Dict, List, Any

class MenuManager:
    """Class to manage interactive menus and user prompts."""
    
    @staticmethod
    def text_prompt(message: str, multiline: bool = False) -> str:
        """Display a prompt for text input.
        
        Args:
            message: The prompt message to display
            multiline: Whether to allow multiline input
            
        Returns:
            The user's input text
        """
        try:
            return prompt(message, multiline=multiline)
        except KeyboardInterrupt:
            print("\nInput cancelled.")
            raise
    
    @staticmethod
    def confirm(message: str, default: bool = False) -> bool:
        """Ask for confirmation from the user.
        
        Args:
            message: The confirmation message
            default: Default value if user just presses Enter
            
        Returns:
            True if user confirms, False otherwise
        """
        default_text = " [Y/n]" if default else " [y/N]"
        try:
            result = prompt(f"{message}{default_text}: ").strip().lower()
            if not result:
                return default
            return result in ('y', 'yes')
        except KeyboardInterrupt:
            print("\nInput cancelled.")
            return False
    
    @staticmethod
    def selection_menu(title: str, options: List[str], 
                      allow_cancel: bool = True) -> int:
        """Display a menu and get user selection.
        
        Args:
            title: The menu title to display
            options: List of options to show
            allow_cancel: Whether to allow cancellation
            
        Returns:
            The selected option index (0-based), or -1 if cancelled
        """
        print(f"\n--- {title} ---")
        for i, option in enumerate(options, 1):
            print(f"{i}: {option}")
        
        if allow_cancel:
            print("Enter 'q' to cancel")
        
        while True:
            try:
                choice = prompt("Choice: ").strip().lower()
                
                if choice in ('q', 'quit') and allow_cancel:
                    return -1
                
                try:
                    index = int(choice) - 1  # Convert to 0-based
                    if 0 <= index < len(options):
                        return index
                    else:
                        print(f"Invalid choice: {choice}. Enter 1-{len(options)}")
                except ValueError:
                    print(f"Invalid input: {choice}")
            except KeyboardInterrupt:
                print("\nSelection cancelled.")
                if allow_cancel:
                    return -1
                # If cancellation not allowed, continue the loop
    
    @staticmethod
    def action_menu(title: str, options: Dict[str, Callable], 
                   allow_exit: bool = True) -> bool:
        """Display a menu and execute selected action.
        
        Args:
            title: The menu title
            options: Dict mapping option text to handler functions
            allow_exit: Whether to include an exit option
            
        Returns:
            True if user chose to exit, False otherwise
        """
        option_list = list(options.keys())
        if allow_exit:
            option_list.append("Exit")
            
        while True:
            index = MenuManager.selection_menu(title, option_list, True)
            
            if index == -1:  # User cancelled
                return True
                
            if allow_exit and index == len(options):  # Exit option
                return True
                
            if 0 <= index < len(options):
                # Get the function for the selected option
                option_key = option_list[index]
                handler = options[option_key]
                
                try:
                    # Execute the handler
                    result = handler()
                    
                    # If handler returns True, exit the menu
                    if result is True:
                        return True
                except KeyboardInterrupt:
                    print("\nOperation cancelled.")
                except Exception as e:
                    print(f"Error: {e}")
            
            # Continue showing menu unless explicitly returned
