from __future__ import annotations
from typing import List, Optional, Tuple
from uuid import UUID, uuid4
from sqlalchemy.orm import Session
from sqlalchemy import select, and_, or_
from fastapi import HTTPException, status
import logging

from app.models.study_model import Study, StudyMember
from app.models.user_model import User
from app.schemas.study_schema import StudyMemberInvite, StudyMemberUpdate, StudyRole
from app.services.email_service import email_service
from app.core.config import settings

logger = logging.getLogger(__name__)

class StudyMemberService:
    def invite_member(self, db: Session, study_id: UUID, inviter: User, payload: StudyMemberInvite) -> StudyMember:
        """
        Invite a member to a study by email.
        """
        # 1. Check if study exists and inviter has permission (only Admin/Creator for now)
        study = db.get(Study, study_id)
        if not study:
            raise HTTPException(status_code=404, detail="Study not found")
        
        if study.creator_id != inviter.id:
            # Get the member record for the inviter
            inviter_member = db.scalars(
                select(StudyMember).where(
                    StudyMember.study_id == study_id,
                    StudyMember.user_id == inviter.id
                )
            ).first()

            if not inviter_member:
                raise HTTPException(status_code=403, detail="Permission denied")

            if inviter_member.role == 'viewer':
                raise HTTPException(status_code=403, detail="Viewers cannot invite members")
            
            if inviter_member.role == 'editor' and payload.role == 'admin':
                raise HTTPException(status_code=403, detail="Editors cannot invite admins")

        # 2. Check if already a member
        existing_member = db.scalars(
            select(StudyMember).where(
                StudyMember.study_id == study_id,
                StudyMember.invited_email == payload.email
            )
        ).first()
        if existing_member:
            raise HTTPException(status_code=400, detail="User is already a member of this study")

        # 3. Check if user exists in the system
        user = db.scalars(
            select(User).where(User.email == payload.email)
        ).first()

        # 4. Create StudyMember record
        new_member = StudyMember(
            id=uuid4(),
            study_id=study_id,
            user_id=user.id if user else None,
            role=payload.role,
            invited_email=payload.email
        )
        db.add(new_member)
        db.commit()
        db.refresh(new_member)

        # 5. Send invitation email
        try:
            if user:
                # Existing user template
                email_service.send_study_invitation(
                    to_email=payload.email,
                    user_name=user.name,
                    study_title=study.title,
                    inviter_name=inviter.name,
                    role=payload.role,
                    is_new_user=False
                )
            else:
                # New user template
                email_service.send_study_invitation(
                    to_email=payload.email,
                    user_name="there",
                    study_title=study.title,
                    inviter_name=inviter.name,
                    role=payload.role,
                    is_new_user=True
                )
        except Exception as e:
            logger.error(f"Failed to send invitation email: {e}")

        if user:
            try:
                from app.services.job_notification_service import create_study_invitation_notification

                create_study_invitation_notification(
                    db,
                    user_id=user.id,
                    study_id=study_id,
                    study_title=study.title or "Untitled Study",
                    inviter_name=inviter.name or inviter.email,
                    role=payload.role,
                    member_id=new_member.id,
                )
            except Exception as e:
                logger.warning("Failed to create study invitation notification: %s", e)

        return new_member

    def list_members(self, db: Session, study_id: UUID) -> List[StudyMember]:
        """
        List all members of a study, including the study creator as an admin.
        The creator will always appear first in the list with role='admin'.
        """
        from sqlalchemy.orm import joinedload
        
        # Fetch the study with creator relationship eagerly loaded
        stmt = select(Study).where(Study.id == study_id).options(joinedload(Study.creator))
        study = db.scalars(stmt).first()
        
        if not study:
            raise HTTPException(status_code=404, detail="Study not found")
        
        logger.info(f"Study creator_id: {study.creator_id}, creator loaded: {study.creator is not None}")
        
        # Fetch all StudyMember records
        members_stmt = select(StudyMember).where(StudyMember.study_id == study_id).order_by(StudyMember.created_at)
        members = list(db.scalars(members_stmt).all())
        
        logger.info(f"Found {len(members)} existing members")
        
        # Helper to get project info if exists
        project = None
        project_creator_id = None
        if study.project_id:
            from app.models.project_model import Project
            project = db.get(Project, study.project_id)
            if project:
                project_creator_id = project.creator_id

        # 1. Handle Study Creator (Owner)
        creator_member = next((m for m in members if m.user_id == study.creator_id), None)
        
        if creator_member:
            members.remove(creator_member)
            
            # Determine role:
            # If no project OR they are the project creator -> 'admin'
            # If they are just a member in the project -> keep existing role (e.g. 'editor')
            target_role = 'admin'
            if project_creator_id and study.creator_id != project_creator_id:
                # Keep their existing role (likely 'editor' synced from project)
                target_role = creator_member.role
            
            # Create synthetic/updated member
            creator_member_obj = StudyMember(
                id=study.creator_id,
                study_id=study_id,
                user_id=study.creator_id,
                role=target_role,
                invited_email=study.creator.email,
                created_at=study.created_at,
                updated_at=study.updated_at
            )
            creator_member_obj.user = study.creator
            members.insert(0, creator_member_obj)
            logger.info(f"Added study creator to list with role {target_role}")
            
        else:
            # Not in list (e.g. standalone study or sync missed) -> force 'admin'
            creator_member_obj = StudyMember(
                id=study.creator_id,
                study_id=study_id,
                user_id=study.creator_id,
                role='admin',
                invited_email=study.creator.email,
                created_at=study.created_at,
                updated_at=study.updated_at
            )
            creator_member_obj.user = study.creator
            members.insert(0, creator_member_obj)
            logger.info(f"Added study creator to list as default admin")

        # 2. Handle Project Creator (if study belongs to a project)
        if project and project_creator_id and project_creator_id != study.creator_id:
            # Check if this project creator is already in the list
            pc_member = next((m for m in members if m.user_id == project_creator_id), None)
            
            if not pc_member:
                # Not in list, add them as synthetic admin
                pc_obj = StudyMember(
                    id=project_creator_id, 
                    study_id=study_id,
                    user_id=project_creator_id,
                    role='admin',
                    invited_email=project.creator.email,
                    created_at=project.created_at, 
                    updated_at=project.updated_at
                )
                pc_obj.user = project.creator
                members.insert(1, pc_obj) # Insert after study creator
                logger.info(f"Added project creator {project.creator.email} to members list as admin")
            else:
                # If present (e.g. synced as viewer?), force display as 'admin' because they own the project
                pc_member.role = 'admin'
        
        return members

    def update_member_role(self, db: Session, study_id: UUID, member_id: UUID, current_user: User, payload: StudyMemberUpdate) -> StudyMember:
        member = db.get(StudyMember, member_id)
        if not member or member.study_id != study_id:
            raise HTTPException(status_code=404, detail="Member not found")
        
        # Check permission: Only study creator or an admin member can change roles
        study = db.get(Study, study_id)
        if study.creator_id != current_user.id:
            admin_check = db.scalars(
                select(StudyMember).where(
                    StudyMember.study_id == study_id,
                    StudyMember.user_id == current_user.id,
                    StudyMember.role == 'admin'
                )
            ).first()
            if not admin_check:
                raise HTTPException(status_code=403, detail="Permission denied")

        member.role = payload.role
        db.commit()
        db.refresh(member)
        return member

    def remove_member(self, db: Session, study_id: UUID, member_id: UUID, current_user: User):
        member = db.get(StudyMember, member_id)
        if not member or member.study_id != study_id:
            raise HTTPException(status_code=404, detail="Member not found")
        
        study = db.get(Study, study_id)
        # Permission: Creator can remove anyone. Admin can remove non-creators. Users can remove themselves.
        is_self = member.user_id == current_user.id
        is_creator = study.creator_id == current_user.id
        
        if not (is_self or is_creator):
            # Check if admin
            admin_check = db.scalars(
                select(StudyMember).where(
                    StudyMember.study_id == study_id,
                    StudyMember.user_id == current_user.id,
                    StudyMember.role == 'admin'
                )
            ).first()
            if not admin_check:
                raise HTTPException(status_code=403, detail="Permission denied")

        db.delete(member)
        db.commit()

study_member_service = StudyMemberService()
