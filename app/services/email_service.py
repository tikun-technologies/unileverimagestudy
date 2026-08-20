import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional
import os
from datetime import datetime
from app.core.config import settings

logger = logging.getLogger(__name__)


class EmailService:
    """Service for sending emails"""
    
    def __init__(self):
        self.smtp_server = settings.SMTP_SERVER
        self.smtp_port = settings.SMTP_PORT
        self.smtp_username = settings.SMTP_USERNAME
        self.smtp_password = settings.SMTP_PASSWORD
        self.from_email = settings.FROM_EMAIL
        self.app_name = "Mindsurve"
        self.frontend_url = settings.FRONTEND_URL
    
    def send_password_reset_email(self, user_email: str, user_name: str, reset_token: str) -> bool:
        """
        Send password reset email to user
        
        Args:
            user_email: User's email address
            user_name: User's name
            reset_token: Password reset token
            
        Returns:
            bool: True if email sent successfully, False otherwise
        """
        try:
            # Create reset link
            reset_link = f"{self.frontend_url}/reset-password?token={reset_token}"
            
            # Create email content
            subject = f"Password Reset Request - {self.app_name}"
            
            html_body = f"""
            <html>
            <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
                <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                    <h2 style="color: #2c3e50;">Password Reset Request</h2>
                    
                    <p>Hello {user_name},</p>
                    
                    <p>We received a request to reset your password for your {self.app_name} account.</p>
                    
                    <p>To reset your password, please click the button below:</p>
                    
                    <div style="text-align: center; margin: 30px 0;">
                        <a href="{reset_link}" 
                           style="background-color: #3498db; color: white; padding: 12px 30px; 
                                  text-decoration: none; border-radius: 5px; display: inline-block;">
                            Reset Password
                        </a>
                    </div>
                    
                    <p>Or copy and paste this link into your browser:</p>
                    <p style="word-break: break-all; background-color: #f8f9fa; padding: 10px; 
                             border-radius: 3px; font-family: monospace;">
                        {reset_link}
                    </p>
                    
                    <p><strong>Important:</strong></p>
                    <ul>
                        <li>This link will expire in 1 hour</li>
                        <li>If you didn't request this password reset, please ignore this email</li>
                        <li>For security reasons, this link can only be used once</li>
                    </ul>
                    
                    <p>If you have any questions, please contact our support team.</p>
                    
                    <hr style="margin: 30px 0; border: none; border-top: 1px solid #eee;">
                    <p style="font-size: 12px; color: #666;">
                        This email was sent from {self.app_name}.<br>
                        If you didn't request this email, please ignore it.
                    </p>
                </div>
            </body>
            </html>
            """
            
            text_body = f"""
            Password Reset Request - {self.app_name}
            
            Hello {user_name},
            
            We received a request to reset your password for your {self.app_name} account.
            
            To reset your password, please visit the following link:
            {reset_link}
            
            Important:
            - This link will expire in 1 hour
            - If you didn't request this password reset, please ignore this email
            - For security reasons, this link can only be used once
            
            If you have any questions, please contact our support team.
            
            ---
            This email was sent from {self.app_name}.
            If you didn't request this email, please ignore it.
            """
            
            # Send email
            return self._send_email(
                to_email=user_email,
                subject=subject,
                html_body=html_body,
                text_body=text_body
            )
            
        except Exception as e:
            logger.error(f"Failed to send password reset email to {user_email}: {str(e)}")
            return False
    
    def _send_email(self, to_email: str, subject: str, html_body: str, text_body: str) -> bool:
        """
        Send email using SMTP
        
        Args:
            to_email: Recipient email address
            subject: Email subject
            html_body: HTML email body
            text_body: Plain text email body
            
        Returns:
            bool: True if email sent successfully, False otherwise
        """
        try:
            if not self.smtp_username or not self.smtp_password:
                logger.error("SMTP credentials not configured")
                return False
            
            # Create message
            msg = MIMEMultipart('alternative')
            msg['From'] = self.from_email
            msg['To'] = to_email
            msg['Subject'] = subject
            
            # Add text and HTML parts
            text_part = MIMEText(text_body, 'plain')
            html_part = MIMEText(html_body, 'html')
            
            msg.attach(text_part)
            msg.attach(html_part)
            
            # Connect to SMTP server and send email
            with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                server.starttls()  # Enable TLS encryption
                server.login(self.smtp_username, self.smtp_password)
                server.send_message(msg)
            
            logger.info(f"Password reset email sent successfully to {to_email}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to send email to {to_email}: {str(e)}")
            return False
    
    def send_welcome_email(self, user_email: str, user_name: str) -> bool:
        """
        Send welcome email to new user
        
        Args:
            user_email: User's email address
            user_name: User's name
            
        Returns:
            bool: True if email sent successfully, False otherwise
        """
        try:
            subject = f"Welcome to {self.app_name}!"
            
            html_body = f"""
            <html>
            <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
                <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                    <h2 style="color: #2c3e50;">Welcome to {self.app_name}!</h2>
                    
                    <p>Hello {user_name},</p>
                    
                    <p>Thank you for registering with {self.app_name}. We're excited to have you on board!</p>
                    
                    <p>You can now start using our platform to create and manage your studies.</p>
                    
                    <div style="text-align: center; margin: 30px 0;">
                        <a href="{self.frontend_url}/login" 
                           style="background-color: #27ae60; color: white; padding: 12px 30px; 
                                  text-decoration: none; border-radius: 5px; display: inline-block;">
                            Get Started
                        </a>
                    </div>
                    
                    <p>If you have any questions, please don't hesitate to contact our support team.</p>
                    
                    <hr style="margin: 30px 0; border: none; border-top: 1px solid #eee;">
                    <p style="font-size: 12px; color: #666;">
                        Welcome to {self.app_name}!
                    </p>
                </div>
            </body>
            </html>
            """
            
            text_body = f"""
            Welcome to {self.app_name}!
            
            Hello {user_name},
            
            Thank you for registering with {self.app_name}. We're excited to have you on board!
            
            You can now start using our platform to create and manage your studies.
            
            Get started: {self.frontend_url}/login
            
            If you have any questions, please don't hesitate to contact our support team.
            
            ---
            Welcome to {self.app_name}!
            """
            
            return self._send_email(
                to_email=user_email,
                subject=subject,
                html_body=html_body,
                text_body=text_body
            )
            
        except Exception as e:
            logger.error(f"Failed to send welcome email to {user_email}: {str(e)}")
            return False

    def send_study_invitation(
        self, 
        to_email: str, 
        user_name: str, 
        study_title: str, 
        inviter_name: str, 
        role: str,
        is_new_user: bool = False
    ) -> bool:
        """
        Send study invitation email
        """
        try:
            subject = f"You've been invited to {study_title} on {self.app_name}"
            
            action_text = "View Study" if not is_new_user else "Create Account & View Study"
            action_url = f"{self.frontend_url}/login"
            
            message = f"You have been invited as a <strong>{role}</strong> to the study <strong>'{study_title}'</strong> by <strong>{inviter_name}</strong>."
            if is_new_user:
                message += "<br><br>It looks like you don't have an account yet. Please create an account using this email to see the study."

            html_body = f"""
            <html>
            <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
                <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                    <h2 style="color: #2c3e50;">Study Invitation</h2>
                    
                    <p>Hello {user_name},</p>
                    
                    <p>{message}</p>
                    
                    <div style="text-align: center; margin: 30px 0;">
                        <a href="{action_url}" 
                           style="background-color: #3498db; color: white; padding: 12px 30px; 
                                   text-decoration: none; border-radius: 5px; display: inline-block;">
                            {action_text}
                        </a>
                    </div>
                    
                    <p>Or copy and paste this link into your browser:</p>
                    <p style="word-break: break-all; background-color: #f8f9fa; padding: 10px; 
                             border-radius: 3px; font-family: monospace;">
                        {action_url}
                    </p>
                    
                    <p>If you have any questions, please contact the person who invited you or our support team.</p>
                    
                    <hr style="margin: 30px 0; border: none; border-top: 1px solid #eee;">
                    <p style="font-size: 12px; color: #666;">
                        This email was sent from {self.app_name}.
                    </p>
                </div>
            </body>
            </html>
            """
            
            text_body = f"""
            Study Invitation - {self.app_name}
            
            Hello {user_name},
            
            You have been invited as a {role} to the study '{study_title}' by {inviter_name}.
            
            {'Please create an account to see the study' if is_new_user else 'You can view the study by logging in'}:
            {action_url}
            
            If you have any questions, please contact the person who invited you or our support team.
            
            ---
            This email was sent from {self.app_name}.
            """
            
            return self._send_email(
                to_email=to_email,
                subject=subject,
                html_body=html_body,
                text_body=text_body
            )
            
        except Exception as e:
            logger.error(f"Failed to send study invitation email to {to_email}: {str(e)}")
            return False

    def send_project_invitation(
        self, 
        to_email: str, 
        user_name: str, 
        project_name: str, 
        inviter_name: str, 
        role: str,
        is_new_user: bool = False
    ) -> bool:
        """
        Send project invitation email
        """
        try:
            subject = f"You have been invited in project {project_name}"
            
            action_text = "View Project" if not is_new_user else "Create Account & View Project"
            action_url = f"{self.frontend_url}/login"
            
            message = f"You have been invited as a <strong>{role}</strong> to the project <strong>'{project_name}'</strong> by <strong>{inviter_name}</strong>."
            message += "<br><br>As a project member, you'll have access to all studies within this project."
            if is_new_user:
                message += "<br><br>It looks like you don't have an account yet. Please create an account using this email to see the project."

            html_body = f"""
            <html>
            <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
                <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                    <h2 style="color: #2c3e50;">Project Invitation</h2>
                    
                    <p>Hello {user_name},</p>
                    
                    <p>{message}</p>
                    
                    <div style="text-align: center; margin: 30px 0;">
                        <a href="{action_url}" 
                           style="background-color: #3498db; color: white; padding: 12px 30px; 
                                   text-decoration: none; border-radius: 5px; display: inline-block;">
                            {action_text}
                        </a>
                    </div>
                    
                    <p>Or copy and paste this link into your browser:</p>
                    <p style="word-break: break-all; background-color: #f8f9fa; padding: 10px; 
                             border-radius: 3px; font-family: monospace;">
                        {action_url}
                    </p>
                    
                    <p>If you have any questions, please contact the person who invited you or our support team.</p>
                    
                    <hr style="margin: 30px 0; border: none; border-top: 1px solid #eee;">
                    <p style="font-size: 12px; color: #666;">
                        This email was sent from {self.app_name}.
                    </p>
                </div>
            </body>
            </html>
            """
            
            text_body = f"""
            Project Invitation - {self.app_name}
            
            Hello {user_name},
            
            You have been invited as a {role} to the project '{project_name}' by {inviter_name}.
            
            As a project member, you'll have access to all studies within this project.
            
            {'Please create an account to see the project' if is_new_user else 'You can view the project by logging in'}:
            {action_url}
            
            If you have any questions, please contact the person who invited you or our support team.
            
            ---
            This email was sent from {self.app_name}.
            """
            
            return self._send_email(
                to_email=to_email,
                subject=subject,
                html_body=html_body,
                text_body=text_body
            )
            
        except Exception as e:
            logger.error(f"Failed to send project invitation email to {to_email}: {str(e)}")
            return False


    def send_export_ready_email(
        self,
        to_email: str,
        user_name: str,
        project_name: str,
        download_url: str,
        filename: str = "export.zip",
    ) -> bool:
        """
        Send email when project export is ready for download.
        
        Args:
            to_email: User's email address
            user_name: User's name
            project_name: Name of the exported project
            download_url: Azure Blob Storage URL with SAS token
            filename: Name of the ZIP file
            
        Returns:
            bool: True if email sent successfully, False otherwise
        """
        try:
            subject = f"Your export is ready: {project_name}"
            
            html_body = f"""
            <html>
            <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
                <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                    <h2 style="color: #2c3e50;">Your Export is Ready!</h2>
                    
                    <p>Hello {user_name},</p>
                    
                    <p>Great news! Your export for <strong>{project_name}</strong> is ready to download.</p>
                    
                    <div style="text-align: center; margin: 30px 0;">
                        <a href="{download_url}" 
                           style="background-color: #27ae60; color: white; padding: 14px 35px; 
                                  text-decoration: none; border-radius: 5px; display: inline-block;
                                  font-weight: bold; font-size: 16px;">
                            Download ZIP
                        </a>
                    </div>
                    
                    <p style="font-size: 13px; color: #666;">
                        <strong>Note:</strong> This download link will expire in 7 days for security reasons.
                    </p>
                    
                    <hr style="margin: 30px 0; border: none; border-top: 1px solid #eee;">
                    <p style="font-size: 12px; color: #666;">
                        This email was sent from {self.app_name}.<br>
                        If you didn't request this export, please ignore this email.
                    </p>
                </div>
            </body>
            </html>
            """
            
            text_body = f"""
            Your Export is Ready! - {self.app_name}
            
            Hello {user_name},
            
            Great news! Your export for {project_name} is ready to download.
            
            Download your file here:
            {download_url}
            
            Note: This download link will expire in 7 days for security reasons.
            
            ---
            This email was sent from {self.app_name}.
            If you didn't request this export, please ignore this email.
            """
            
            return self._send_email(
                to_email=to_email,
                subject=subject,
                html_body=html_body,
                text_body=text_body
            )
            
        except Exception as e:
            logger.error(f"Failed to send export ready email to {to_email}: {str(e)}")
            return False

    def send_task_generation_complete_email(
        self,
        to_email: str,
        user_name: str,
        study_title: str,
        study_url: str,
        respondents_count: int | None = None,
    ) -> bool:
        """Send email when background task generation completes successfully."""
        try:
            subject = f"Tasks generated: {study_title}"

            detail_line = ""
            if respondents_count and respondents_count > 0:
                detail_line = f"<p>Tasks were generated for <strong>{respondents_count}</strong> respondent(s).</p>"

            html_body = f"""
            <html>
            <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
                <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                    <h2 style="color: #2674BA;">Task generation complete</h2>

                    <p>Hello {user_name},</p>

                    <p>Task generation for <strong>{study_title}</strong> has finished successfully.</p>

                    {detail_line}

                    <div style="text-align: center; margin: 30px 0;">
                        <a href="{study_url}"
                           style="background-color: #2674BA; color: white; padding: 14px 35px;
                                  text-decoration: none; border-radius: 5px; display: inline-block;
                                  font-weight: bold; font-size: 16px;">
                            View study
                        </a>
                    </div>

                    <hr style="margin: 30px 0; border: none; border-top: 1px solid #eee;">
                    <p style="font-size: 12px; color: #666;">
                        This email was sent from {self.app_name}.<br>
                        If you did not start this task generation, you can ignore this email.
                    </p>
                </div>
            </body>
            </html>
            """

            respondents_text = (
                f"Tasks were generated for {respondents_count} respondent(s).\n\n"
                if respondents_count and respondents_count > 0
                else ""
            )

            text_body = f"""
            Task generation complete - {self.app_name}

            Hello {user_name},

            Task generation for {study_title} has finished successfully.

            {respondents_text}View your study here:
            {study_url}

            ---
            This email was sent from {self.app_name}.
            If you did not start this task generation, you can ignore this email.
            """

            return self._send_email(
                to_email=to_email,
                subject=subject,
                html_body=html_body,
                text_body=text_body,
            )
        except Exception as e:
            logger.error(f"Failed to send task generation email to {to_email}: {str(e)}")
            return False

    def send_synthetic_simulation_complete_email(
        self,
        to_email: str,
        user_name: str,
        study_title: str,
        study_url: str,
        respondents_count: int | None = None,
    ) -> bool:
        """Send email when background synthetic simulation completes successfully."""
        try:
            subject = f"Synthetic simulation complete: {study_title}"

            detail_line = ""
            if respondents_count and respondents_count > 0:
                detail_line = f"<p><strong>{respondents_count}</strong> synthetic respondent(s) finished your study.</p>"

            html_body = f"""
            <html>
            <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
                <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                    <h2 style="color: #2674BA;">Synthetic simulation complete</h2>

                    <p>Hello {user_name},</p>

                    <p>Your synthetic simulation for <strong>{study_title}</strong> has finished successfully.</p>

                    {detail_line}

                    <div style="text-align: center; margin: 30px 0;">
                        <a href="{study_url}"
                           style="background-color: #2674BA; color: white; padding: 14px 35px;
                                  text-decoration: none; border-radius: 5px; display: inline-block;
                                  font-weight: bold; font-size: 16px;">
                            View study
                        </a>
                    </div>

                    <hr style="margin: 30px 0; border: none; border-top: 1px solid #eee;">
                    <p style="font-size: 12px; color: #666;">
                        This email was sent from {self.app_name}.<br>
                        If you did not start this simulation, you can ignore this email.
                    </p>
                </div>
            </body>
            </html>
            """

            respondents_text = (
                f"{respondents_count} synthetic respondent(s) finished your study.\n\n"
                if respondents_count and respondents_count > 0
                else ""
            )

            text_body = f"""
            Synthetic simulation complete - {self.app_name}

            Hello {user_name},

            Your synthetic simulation for {study_title} has finished successfully.

            {respondents_text}View your study here:
            {study_url}

            ---
            This email was sent from {self.app_name}.
            If you did not start this simulation, you can ignore this email.
            """

            return self._send_email(
                to_email=to_email,
                subject=subject,
                html_body=html_body,
                text_body=text_body,
            )
        except Exception as e:
            logger.error(f"Failed to send synthetic simulation email to {to_email}: {str(e)}")
            return False


# Global email service instance
email_service = EmailService()
