# app/services/project_member_service.py
from __future__ import annotations
from typing import List, Optional
from uuid import UUID, uuid4
from sqlalchemy.orm import Session
from sqlalchemy import select
from fastapi import HTTPException, status
import logging

from app.models.project_model import Project, ProjectMember
from app.models.study_model import Study, StudyMember
from app.models.user_model import User
from app.schemas.project_schema import ProjectMemberInvite, ProjectMemberUpdate
from app.services.email_service import email_service

logger = logging.getLogger(__name__)


class ProjectMemberService:
    def invite_member(
        self, 
        db: Session, 
        project_id: UUID, 
        inviter: User, 
        payload: ProjectMemberInvite
    ) -> ProjectMember:
        """
        Invite a member to a project by email.
        Automatically adds them to all existing studies in the project.
        """
        # 1. Check if project exists and inviter has permission
        project = db.get(Project, project_id)
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        
        # Check permission: Only project creator OR editors can invite members
        is_owner = project.creator_id == inviter.id
        is_editor = False
        
        if not is_owner:
            # Check if inviter is an editor
            inviter_member = db.scalars(
                select(ProjectMember).where(
                    ProjectMember.project_id == project_id,
                    ProjectMember.user_id == inviter.id,
                    ProjectMember.role == 'editor'
                )
            ).first()
            if inviter_member:
                is_editor = True
        
        if not (is_owner or is_editor):
            raise HTTPException(status_code=403, detail="Only project owner or editors can invite members")

        # 2. Validate role
        if payload.role not in ['editor', 'viewer']:
            raise HTTPException(status_code=400, detail="Role must be 'editor' or 'viewer'")

        # 3. Check if already a member
        existing_member = db.scalars(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.invited_email == payload.email
            )
        ).first()
        if existing_member:
            raise HTTPException(status_code=400, detail="User is already a member of this project")

        # 4. Check if user exists in the system
        user = db.scalars(
            select(User).where(User.email == payload.email)
        ).first()

        # 5. Create ProjectMember record
        new_member = ProjectMember(
            id=uuid4(),
            project_id=project_id,
            user_id=user.id if user else None,
            role=payload.role,
            invited_email=payload.email
        )
        db.add(new_member)
        db.flush()  # Flush to get the ID, but don't commit yet

        # 6. Automatically add member to all existing studies in the project
        self._sync_member_to_all_studies(db, project_id, new_member)

        db.commit()
        db.refresh(new_member)

        # 7. Send invitation email
        try:
            if user:
                email_service.send_project_invitation(
                    to_email=payload.email,
                    user_name=user.name,
                    project_name=project.name,
                    inviter_name=inviter.name,
                    role=payload.role,
                    is_new_user=False
                )
            else:
                email_service.send_project_invitation(
                    to_email=payload.email,
                    user_name="there",
                    project_name=project.name,
                    inviter_name=inviter.name,
                    role=payload.role,
                    is_new_user=True
                )
        except Exception as e:
            logger.error(f"Failed to send project invitation email: {e}")

        if user:
            try:
                from app.services.job_notification_service import create_project_invitation_notification

                create_project_invitation_notification(
                    db,
                    user_id=user.id,
                    project_id=project_id,
                    project_name=project.name or "Untitled Project",
                    inviter_name=inviter.name or inviter.email,
                    role=payload.role,
                    member_id=new_member.id,
                )
            except Exception as e:
                logger.warning("Failed to create project invitation notification: %s", e)

        return new_member

    def _sync_member_to_all_studies(
        self, 
        db: Session, 
        project_id: UUID, 
        project_member: ProjectMember
    ):
        """
        Add a project member to all studies in the project.
        Skips studies where the user is already a member.
        """
        # Get all studies in this project
        studies = db.scalars(
            select(Study).where(Study.project_id == project_id)
        ).all()

        for study in studies:
            # Check if already a member of this study
            existing_study_member = db.scalars(
                select(StudyMember).where(
                    StudyMember.study_id == study.id,
                    StudyMember.invited_email == project_member.invited_email
                )
            ).first()

            if not existing_study_member:
                # Add to study with the same role
                study_member = StudyMember(
                    id=uuid4(),
                    study_id=study.id,
                    user_id=project_member.user_id,
                    role=project_member.role,
                    invited_email=project_member.invited_email
                )
                db.add(study_member)

        logger.info(f"Added project member {project_member.invited_email} to {len(studies)} studies")

    def sync_new_study_to_project_members(
        self, 
        db: Session, 
        study_id: UUID, 
        project_id: UUID
    ):
        """
        When a new study is created in a project, add all project members to it.
        Called from study creation service.
        """
        # 1. First, ensure Project Creator is added as Admin to the study
        # (unless they are the ones creating it, in which case they are handled by study creation, 
        # but adding a record doesn't hurt and ensures consistency if logic changes)
        project = db.get(Project, project_id)
        if project:
            creator_email = db.execute(
                select(User.email).where(User.id == project.creator_id)
            ).scalar_one_or_none()

            # Check if project creator is already a member of the study
            existing_creator = db.scalars(
                select(StudyMember).where(
                    StudyMember.study_id == study_id,
                    StudyMember.user_id == project.creator_id
                )
            ).first()
            
            if not existing_creator:
                # Add project creator as admin
                creator_member = StudyMember(
                    id=uuid4(),
                    study_id=study_id,
                    user_id=project.creator_id,
                    role='admin',
                    invited_email=creator_email or ""
                )
                db.add(creator_member)
                logger.info(f"Added project creator {creator_email or project.creator_id} to new study {study_id} as admin")

        # 2. Add other project members
        project_members = db.scalars(
            select(ProjectMember).where(ProjectMember.project_id == project_id)
        ).all()

        for pm in project_members:
            # Check if already a member
            existing = db.scalars(
                select(StudyMember).where(
                    StudyMember.study_id == study_id,
                    StudyMember.invited_email == pm.invited_email
                )
            ).first()

            if not existing:
                study_member = StudyMember(
                    id=uuid4(),
                    study_id=study_id,
                    user_id=pm.user_id,
                    role=pm.role,
                    invited_email=pm.invited_email
                )
                db.add(study_member)

        logger.info(f"Added {len(project_members)} project members to new study {study_id}")

    def list_members(self, db: Session, project_id: UUID) -> List[ProjectMember]:
        """
        List all members of a project, including the project creator as owner.
        """
        from sqlalchemy.orm import joinedload
        
        # Fetch the project with creator relationship
        stmt = select(Project).where(Project.id == project_id).options(joinedload(Project.creator))
        project = db.scalars(stmt).first()
        
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        
        # Fetch all ProjectMember records
        members_stmt = select(ProjectMember).where(
            ProjectMember.project_id == project_id
        ).order_by(ProjectMember.created_at)
        members = list(db.scalars(members_stmt).all())
        
        # Find if creator is already in the members list
        creator_member = next((m for m in members if m.user_id == project.creator_id), None)
        
        if creator_member:
            # If creator is already a member, remove them so we can re-insert at front with 'owner' role
            members.remove(creator_member)
        
        # Create/Update synthetic ProjectMember for creator with 'admin' role
        # We always return 'admin' role for the creator regardless of what's in DB
        owner_member = ProjectMember(
            id=project.creator_id,
            project_id=project_id,
            user_id=project.creator_id,
            role='admin',  # Creator is the admin
            invited_email=project.creator.email,
            created_at=project.created_at,
            updated_at=project.updated_at
        )
        owner_member.user = project.creator
        
        # Insert owner at the beginning of the list
        members.insert(0, owner_member)
        
        return members

    def update_member_role(
        self, 
        db: Session, 
        project_id: UUID, 
        member_id: UUID, 
        current_user: User, 
        payload: ProjectMemberUpdate
    ) -> ProjectMember:
        """
        Update a project member's role.
        Also updates their role in all studies in the project.
        """
        member = db.get(ProjectMember, member_id)
        if not member or member.project_id != project_id:
            raise HTTPException(status_code=404, detail="Member not found")
        
        # Check permission: Only project creator can change roles
        project = db.get(Project, project_id)
        if project.creator_id != current_user.id:
            raise HTTPException(status_code=403, detail="Only project owner can update member roles")

        # Validate role
        if payload.role not in ['editor', 'viewer']:
            raise HTTPException(status_code=400, detail="Role must be 'editor' or 'viewer'")

        # Update project member role
        old_role = member.role
        member.role = payload.role
        
        # Update role in all studies in the project
        if old_role != payload.role:
            # Get all studies in this project
            studies = db.scalars(
                select(Study.id).where(Study.project_id == project_id)
            ).all()
            
            if studies:
                # Update study members for all project studies
                from sqlalchemy import update
                stmt = (
                    update(StudyMember)
                    .where(
                        StudyMember.study_id.in_(studies),
                        StudyMember.invited_email == member.invited_email
                    )
                    .values(role=payload.role)
                )
                db.execute(stmt)
        
        db.commit()
        db.refresh(member)
        
        logger.info(f"Updated member {member.invited_email} role from {old_role} to {payload.role} in project and all studies")
        
        return member

    def remove_member(
        self, 
        db: Session, 
        project_id: UUID, 
        member_id: UUID, 
        current_user: User
    ):
        """
        Remove a member from the project.
        For studies they created: keep access but demote to viewer.
        For other studies in the project: remove access (delete StudyMember).
        """
        member = db.get(ProjectMember, member_id)
        if not member or member.project_id != project_id:
            raise HTTPException(status_code=404, detail="Member not found")
        
        project = db.get(Project, project_id)
        
        # Permission: Only project creator can remove members
        if project.creator_id != current_user.id:
            raise HTTPException(status_code=403, detail="Only project owner can remove members")

        # Get all study IDs for this project
        study_ids = db.scalars(
            select(Study.id).where(Study.project_id == project_id)
        ).all()

        # Studies where this member is the creator (only if they have a user_id)
        study_ids_created_by_member = set()
        if member.user_id and study_ids:
            study_ids_created_by_member = set(
                db.scalars(
                    select(Study.id).where(
                        Study.project_id == project_id,
                        Study.creator_id == member.user_id,
                    )
                ).all()
            )

        study_members_deleted = 0
        study_members_demoted = 0
        if study_ids:
            # Demote to viewer for studies they created (keep access, view-only)
            if study_ids_created_by_member:
                demoted = db.query(StudyMember).filter(
                    StudyMember.study_id.in_(study_ids_created_by_member),
                    StudyMember.invited_email == member.invited_email,
                ).update({StudyMember.role: "viewer"}, synchronize_session=False)
                study_members_demoted = demoted

            # Remove from studies they did not create
            delete_ids = [s for s in study_ids if s not in study_ids_created_by_member]
            if delete_ids:
                study_members_deleted = db.query(StudyMember).filter(
                    StudyMember.study_id.in_(delete_ids),
                    StudyMember.invited_email == member.invited_email,
                ).delete(synchronize_session=False)

        # Remove from project
        db.delete(member)
        db.commit()
        
        logger.info(
            f"Removed member {member.invited_email} from project; "
            f"demoted to viewer in {study_members_demoted} study(ies), removed from {study_members_deleted} study(ies)"
        )


project_member_service = ProjectMemberService()
